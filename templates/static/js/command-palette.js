/**
 * Command palette — the navigation surface.
 *
 * With no persistent nav, this is the only thing standing between an operator
 * and 19 destinations, so it carries two jobs that pull in different
 * directions:
 *
 *   1. FAST for someone who knows where they are going. Two keys and a
 *      fragment of a name.
 *   2. A MAP for someone who does not. Opening it with an empty query must
 *      show everything the system can do, grouped, because nothing else on
 *      screen advertises those features any more.
 *
 * Job 2 is why the empty state lists all destinations instead of only recents:
 * a palette that starts blank would make the whole product invisible.
 */
const CommandPalette = (function () {
    const RECENT_KEY = 'ww-palette-recent';
    const MAX_RECENT = 5;

    // Grouped so the empty state reads as a table of contents. Keywords carry
    // the synonyms an operator would actually type, including Chinese: this
    // team runs a Chinese-language operation and "静音" must find Silences.
    const GROUPS = [
        {
            title: 'nav.group.overview',
            items: [
                { slug: 'overview', icon: '\u{1F4CA}', label: 'nav.dest.overview', keywords: 'overview home stats 总览 概览 首页' },
                { slug: 'trace', icon: '\u{1F50E}', label: 'nav.dest.trace', keywords: 'decision trace why skipped 决策链 为什么 抑制' },
                { slug: 'cost', icon: '\u{1F4B0}', label: 'nav.dest.cost', keywords: 'ai cost spend token 成本 花费' },
            ],
        },
        {
            title: 'nav.group.inbox',
            items: [
                { slug: 'alerts', icon: '\u{1F514}', label: 'nav.dest.alerts', keywords: 'alerts events inbox 告警 事件 收件箱' },
                { slug: 'work-queue', icon: '\u{1F4CB}', label: 'nav.dest.workQueue', keywords: 'work queue triage sla 工作队列 待办' },
                { slug: 'incidents', icon: '\u{1F525}', label: 'nav.dest.incidents', keywords: 'incidents outage 事件单 故障' },
                { slug: 'investigations', icon: '\u{1F9EA}', label: 'nav.dest.investigations', keywords: 'deep analysis investigations 调查 深度分析' },
            ],
        },
        {
            title: 'nav.group.routing',
            items: [
                { slug: 'rules', icon: '⚙️', label: 'nav.dest.rules', keywords: 'forward rules routing targets 转发 规则 路由' },
                { slug: 'silences', icon: '\u{1F515}', label: 'nav.dest.silences', keywords: 'silence mute maintenance 静音 屏蔽 维护窗' },
                { slug: 'sandbox', icon: '\u{1F9EB}', label: 'nav.dest.sandbox', keywords: 'sandbox test payload dry run 沙箱 测试' },
                { slug: 'audit', icon: '\u{1F4DC}', label: 'nav.dest.audit', keywords: 'audit rule history 审计 变更' },
                { slug: 'ingress', icon: '\u{1F4E5}', label: 'nav.dest.ingress', keywords: 'inbound setup webhook source 接入 来源' },
                { slug: 'quality', icon: '✨', label: 'nav.dest.quality', keywords: 'alert quality schema 质量 数据质量' },
                { slug: 'integrations', icon: '\u{1F517}', label: 'nav.dest.integrations', keywords: 'integrations feishu lark 集成 飞书' },
            ],
        },
        {
            title: 'nav.group.operations',
            items: [
                { slug: 'actions', icon: '\u{1F6E0}️', label: 'nav.dest.actions', keywords: 'action center queue todo 行动 待处理' },
                { slug: 'noise', icon: '\u{1F507}', label: 'nav.dest.noise', keywords: 'noise reduction dedup 降噪 噪音' },
                { slug: 'kb', icon: '\u{1F4D6}', label: 'nav.dest.kb', keywords: 'knowledge base drafts runbook 知识库 草稿' },
                { slug: 'gaps', icon: '\u{1F573}️', label: 'nav.dest.gaps', keywords: 'knowledge gaps missing 知识缺口' },
                { slug: 'settings', icon: '\u{1F39B}️', label: 'nav.dest.settings', keywords: 'runtime settings policy config 设置 配置 策略' },
            ],
        },
    ];

    const ALL = GROUPS.flatMap((group) => group.items.map((item) => ({ ...item, group: group.title })));

    let open = false;
    let matches = [];
    let cursor = 0;

    function label(item) {
        const translated = typeof t === 'function' ? t(item.label) : item.label;
        // Fall back to the slug rather than showing a raw i18n key: a missing
        // translation must not make a destination unrecognisable.
        return translated === item.label ? item.slug : translated;
    }

    function recents() {
        try {
            const stored = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
            return Array.isArray(stored) ? stored.filter((slug) => ALL.some((i) => i.slug === slug)) : [];
        } catch (error) {
            return [];
        }
    }

    function remember(slug) {
        try {
            const next = [slug, ...recents().filter((s) => s !== slug)].slice(0, MAX_RECENT);
            localStorage.setItem(RECENT_KEY, JSON.stringify(next));
        } catch (error) {
            /* private mode: recents are a convenience, never a requirement */
        }
    }

    /** Subsequence match, so "wq" finds "work-queue" and "jc" finds 决策链. */
    function score(item, query) {
        if (!query) return 0;
        const haystack = (label(item) + ' ' + item.slug + ' ' + item.keywords).toLowerCase();
        if (haystack.includes(query)) return 100 - haystack.indexOf(query);
        let index = 0;
        for (const char of query) {
            index = haystack.indexOf(char, index);
            if (index === -1) return -1;
            index += 1;
        }
        return 1;
    }

    function compute(query) {
        const normalized = String(query || '').trim().toLowerCase();
        if (!normalized) {
            const recent = recents();
            // Recents first, then the full map — never a blank panel.
            return [
                ...recent.map((slug) => ALL.find((i) => i.slug === slug)).filter(Boolean).map((i) => ({ ...i, recent: true })),
                ...ALL.filter((i) => !recent.includes(i.slug)),
            ];
        }
        return ALL
            .map((item) => ({ item, rank: score(item, normalized) }))
            .filter((entry) => entry.rank >= 0)
            .sort((a, b) => b.rank - a.rank)
            .map((entry) => entry.item);
    }

    function render() {
        const list = document.getElementById('paletteList');
        if (!list) return;
        if (!matches.length) {
            list.innerHTML = '<div class="palette-empty">' + escapeHtml(t('palette.noMatch')) + '</div>';
            return;
        }
        let html = '';
        let lastGroup = null;
        matches.forEach(function (item, index) {
            const group = item.recent ? 'palette.recent' : item.group;
            if (group !== lastGroup) {
                html += '<div class="palette-group">' + escapeHtml(t(group)) + '</div>';
                lastGroup = group;
            }
            html += '<button type="button" class="palette-item' + (index === cursor ? ' is-active' : '') +
                '" data-palette-index="' + index + '" role="option" aria-selected="' + (index === cursor) + '">' +
                '<span class="palette-icon">' + item.icon + '</span>' +
                '<span class="palette-label">' + escapeHtml(label(item)) + '</span>' +
                '<span class="palette-slug">#/' + escapeHtml(item.slug) + '</span></button>';
        });
        list.innerHTML = html;
        const active = list.querySelector('.palette-item.is-active');
        if (active) active.scrollIntoView({ block: 'nearest' });
    }

    function move(delta) {
        if (!matches.length) return;
        cursor = (cursor + delta + matches.length) % matches.length;
        render();
    }

    function choose(index) {
        const item = matches[typeof index === 'number' ? index : cursor];
        if (!item) return;
        remember(item.slug);
        close();
        navigateTo(item.slug);
    }

    function show() {
        const overlay = document.getElementById('commandPalette');
        const input = document.getElementById('paletteInput');
        if (!overlay || !input) return;
        open = true;
        overlay.classList.add('is-open');
        overlay.setAttribute('aria-hidden', 'false');
        input.value = '';
        matches = compute('');
        cursor = 0;
        render();
        input.focus();
    }

    function close() {
        const overlay = document.getElementById('commandPalette');
        if (!overlay) return;
        open = false;
        overlay.classList.remove('is-open');
        overlay.setAttribute('aria-hidden', 'true');
    }

    function isTypingTarget(target) {
        if (!target) return false;
        const tag = String(target.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
    }

    function bind() {
        document.addEventListener('keydown', function (event) {
            const meta = event.metaKey || event.ctrlKey;
            if (meta && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                open ? close() : show();
                return;
            }
            // "/" is the other muscle memory for search, but only when the user
            // is not already typing into a field.
            if (!open && event.key === '/' && !isTypingTarget(event.target)) {
                event.preventDefault();
                show();
                return;
            }
            if (!open) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                close();
            } else if (event.key === 'ArrowDown') {
                event.preventDefault();
                move(1);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                move(-1);
            } else if (event.key === 'Enter') {
                event.preventDefault();
                choose();
            }
        });

        const input = document.getElementById('paletteInput');
        if (input) {
            input.addEventListener('input', function () {
                matches = compute(input.value);
                cursor = 0;
                render();
            });
        }

        const overlay = document.getElementById('commandPalette');
        if (overlay) {
            overlay.addEventListener('click', function (event) {
                // Backdrop click closes; a click inside the panel must not.
                if (event.target === overlay) close();
                const button = event.target.closest('[data-palette-index]');
                if (button) choose(Number(button.getAttribute('data-palette-index')));
            });
        }

        // Mouse and touch users have no keyboard shortcut; the trigger in the
        // top bar is their only way in, so it is not optional.
        document.querySelectorAll('[data-palette-open]').forEach(function (trigger) {
            trigger.addEventListener('click', show);
        });
    }

    return {
        init: bind,
        open: show,
        close: close,
        // Exposed for tests and for anything that needs the destination map.
        // The sidebar renders from these same groups: one source of truth, so
        // the palette and the persistent nav cannot drift apart.
        _all: ALL,
        _groups: GROUPS,
        _compute: compute,
    };
})();
