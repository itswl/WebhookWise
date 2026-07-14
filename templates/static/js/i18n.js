/**
 * Lightweight client-side i18n for the dashboard.
 *
 * - Two dictionaries (zh / en) live in i18n.zh.js / i18n.en.js and register
 *   themselves onto window.__WW_I18N_DICT__; `t(key, params)` looks up the
 *   active language and falls back to the key itself if a string is missing.
 * - Only the active language is downloaded on first paint; the other is fetched
 *   on demand the first time the user toggles (see ensureDict / setLang).
 * - Active language is persisted in localStorage; the default follows the
 *   browser language (zh-* -> zh, otherwise en).
 * - Static markup is annotated with data-i18n* attributes and updated by
 *   applyStaticTranslations(); dynamic JS-rendered strings call t(...).
 * - Switching language re-applies static text, updates <html lang>, and asks
 *   registered listeners (the dashboard) to re-render the active tab.
 *
 * This only localizes the UI chrome (labels, buttons, empty states, headers).
 * Data the backend produces — alert summaries, AI analysis, Feishu cards — is
 * NOT translated here; its language is decided by the alert source and prompts.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'dashboard_lang';
    var SUPPORTED = ['zh', 'en'];
    var DEFAULT_LANG = 'en';

    // Dictionaries register themselves onto this shared global from their own
    // per-language files. Referencing the same object means a dictionary that
    // loads after this core is picked up without any extra wiring.
    var DICT = (window.__WW_I18N_DICT__ = window.__WW_I18N_DICT__ || {});

    // Build the per-language dictionary URL. The server-rendered dashboard
    // publishes runtime asset versions in <body data-asset-versions> (a CSP-safe
    // alternative to an inline manifest script) so lazily-loaded dict files carry
    // the same content-hash cache-busting as statically referenced assets.
    function assetVersions() {
        try {
            var raw = document.body && document.body.getAttribute('data-asset-versions');
            return raw ? JSON.parse(raw) : {};
        } catch (e) {
            return {};
        }
    }

    function dictUrl(lang) {
        var v = assetVersions()['i18n.' + lang + '.js'];
        return '/static/js/i18n.' + lang + '.js' + (v ? '?v=' + v : '');
    }

    var _dictPromises = {};
    function ensureDict(lang) {
        if (DICT[lang]) return Promise.resolve();
        if (_dictPromises[lang]) return _dictPromises[lang];
        _dictPromises[lang] = new Promise(function (resolve) {
            var script = document.createElement('script');
            script.src = dictUrl(lang);
            // Resolve on error too: t() degrades to the key rather than hanging.
            script.onload = function () { resolve(); };
            script.onerror = function () {
                console.error('i18n dictionary failed to load: ' + lang);
                resolve();
            };
            document.head.appendChild(script);
        });
        return _dictPromises[lang];
    }

    function normalizeLang(lang) {
        if (!lang) return null;
        var lower = String(lang).toLowerCase();
        if (lower.indexOf('zh') === 0) return 'zh';
        if (lower.indexOf('en') === 0) return 'en';
        return SUPPORTED.indexOf(lower) >= 0 ? lower : null;
    }

    function detectDefault() {
        try {
            var stored = localStorage.getItem(STORAGE_KEY);
            var norm = normalizeLang(stored);
            if (norm) return norm;
        } catch (e) { /* localStorage unavailable */ }
        var nav = (navigator.languages && navigator.languages[0]) || navigator.language;
        return normalizeLang(nav) || DEFAULT_LANG;
    }

    var currentLang = detectDefault();
    var listeners = [];

    // Start loading the active language immediately. The dashboard awaits this
    // (I18N.ready) before applying static translations so the first paint is
    // never a flash of raw keys.
    var ready = ensureDict(currentLang);

    function t(key, params) {
        var table = DICT[currentLang] || DICT[DEFAULT_LANG] || {};
        var fallback = DICT[DEFAULT_LANG] || {};
        var value = (table[key] != null) ? table[key]
            : (fallback[key] != null ? fallback[key] : key);
        if (params) {
            value = value.replace(/\{(\w+)\}/g, function (m, name) {
                return params[name] != null ? params[name] : m;
            });
        }
        return value;
    }

    /** Apply translations to all annotated nodes in the (sub)tree. */
    function applyStaticTranslations(root) {
        root = root || document;
        root.querySelectorAll('[data-i18n]').forEach(function (el) {
            el.textContent = t(el.getAttribute('data-i18n'));
        });
        root.querySelectorAll('[data-i18n-html]').forEach(function (el) {
            el.innerHTML = t(el.getAttribute('data-i18n-html'));
        });
        root.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
        });
        root.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
        });
        root.querySelectorAll('[data-i18n-aria-label]').forEach(function (el) {
            el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria-label')));
        });
    }

    function getLang() { return currentLang; }

    function setLang(lang) {
        var norm = normalizeLang(lang);
        if (!norm || norm === currentLang) {
            if (norm) { applyStaticTranslations(); }
            return;
        }
        // Lazy-load the target language's dictionary before switching so the
        // first toggle never renders raw keys.
        ensureDict(norm).then(function () {
            currentLang = norm;
            try { localStorage.setItem(STORAGE_KEY, norm); } catch (e) { /* ignore */ }
            document.documentElement.setAttribute('lang', norm === 'zh' ? 'zh-CN' : 'en');
            applyStaticTranslations();
            listeners.forEach(function (fn) {
                try { fn(norm); } catch (e) { console.error('i18n listener failed', e); }
            });
        });
    }

    function toggleLang() {
        setLang(currentLang === 'zh' ? 'en' : 'zh');
    }

    /** Register a callback fired after the language changes (re-render hook). */
    function onChange(fn) {
        if (typeof fn === 'function') listeners.push(fn);
    }

    window.I18N = {
        t: t,
        getLang: getLang,
        setLang: setLang,
        toggleLang: toggleLang,
        onChange: onChange,
        apply: applyStaticTranslations,
        // Resolves once the active language's dictionary has loaded.
        ready: ready
    };
    // Convenience global so module code can call t('key') directly.
    window.t = t;

    // Set <html lang> as early as possible to match the detected language.
    document.documentElement.setAttribute('lang', currentLang === 'zh' ? 'zh-CN' : 'en');
})();
