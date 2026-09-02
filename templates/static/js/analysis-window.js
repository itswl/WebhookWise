/**
 * Shared analysis time window.
 *
 * Decision Trace, AI Cost, Alert Quality, Rule Audit and Noise Center each
 * used to keep a private time range: an operator who set "30 days" on the
 * trace page landed on the audit page looking at a different month, and
 * nothing said so. The five now sit in one sidebar group and read one window.
 *
 * Model: a period ('day' | 'week' | 'month') persisted under ONE localStorage
 * key. Pages with Day/Week/Month buttons write and read the period directly.
 * Pages with a day-count <select> map through it — 1 → day, 7 → week,
 * 30 → month — while values with no mapping (90, 180) stay local to that page
 * and are never persisted, so a select keeps every option it had.
 *
 * Loads before every analysis module (see the <script> order in
 * dashboard.html); callers still guard on `typeof wwAnalysisWindow` so a
 * harness that slices one module runs without it.
 */
var wwAnalysisWindow = (function () {
    'use strict';

    var STORAGE_KEY = 'ww-analysis-window';
    var DEFAULT_PERIOD = 'day';
    var DAYS = { day: 1, week: 7, month: 30 };
    // Marks the shared period a <select> was last reconciled with, so a
    // local unmapped pick survives refreshes yet yields to a NEWER shared
    // choice made on another page.
    var SEEN_ATTR = 'data-ww-window-seen';
    var BOUND_ATTR = 'data-ww-window-bound';
    // In-memory mirror: private mode can refuse localStorage, and the window
    // must still hold together within one page load.
    var memory = null;

    function normalize(period) {
        return Object.prototype.hasOwnProperty.call(DAYS, String(period)) ? String(period) : null;
    }

    function stored() {
        try {
            var value = normalize(localStorage.getItem(STORAGE_KEY));
            if (value) return value;
        } catch (e) { /* private mode */ }
        return memory;
    }

    function describe(period) {
        return { period: period, days: DAYS[period] };
    }

    /** The shared window; the landing default (today) when nobody chose one yet. */
    function get() {
        return describe(stored() || DEFAULT_PERIOD);
    }

    /** True once an operator has chosen a window on any analysis page. */
    function isSet() {
        return stored() !== null;
    }

    /** Persist a period; anything outside day/week/month is ignored. */
    function set(period) {
        var norm = normalize(period);
        if (!norm) return null;
        memory = norm;
        try { localStorage.setItem(STORAGE_KEY, norm); } catch (e) { /* private mode */ }
        return describe(norm);
    }

    /** 1 → 'day', 7 → 'week', 30 → 'month'; anything else → null. */
    function fromDays(days) {
        var n = Number(days);
        for (var period in DAYS) {
            if (Object.prototype.hasOwnProperty.call(DAYS, period) && DAYS[period] === n) return period;
        }
        return null;
    }

    function toDays(period) {
        var norm = normalize(period);
        return norm ? DAYS[norm] : null;
    }

    function hasOption(select, value) {
        var options = select.options || [];
        for (var i = 0; i < options.length; i++) {
            if (String(options[i].value) === value) return true;
        }
        return false;
    }

    /**
     * Bring a day-count <select> in line with the shared window.
     *
     * Applies the shared period only when it changed since this select last
     * saw it: an operator who picked an unmapped 90 days keeps it across
     * refreshes of that page, but a window chosen on another page afterwards
     * wins. A select with no option for the shared day count is left alone.
     * Returns the period applied, or null when nothing changed.
     */
    function syncSelect(select) {
        if (!select) return null;
        var period = stored();
        if (!period) return null;
        if (select.getAttribute(SEEN_ATTR) === period) return null;
        select.setAttribute(SEEN_ATTR, period);
        var days = String(DAYS[period]);
        if (!hasOption(select, days)) return null;
        select.value = days;
        return period;
    }

    /** Persist a select's value when it maps to a period; unmapped stays local. */
    function adoptSelect(select) {
        if (!select) return null;
        var period = fromDays(select.value);
        if (!period) return null;
        set(period);
        select.setAttribute(SEEN_ATTR, period);
        return period;
    }

    /** Wire adoptSelect to the select's change event, once. */
    function bindSelect(select) {
        if (!select || typeof select.addEventListener !== 'function') return;
        if (select.getAttribute(BOUND_ATTR) === '1') return;
        select.setAttribute(BOUND_ATTR, '1');
        select.addEventListener('change', function () { adoptSelect(select); });
    }

    return {
        STORAGE_KEY: STORAGE_KEY,
        get: get,
        set: set,
        isSet: isSet,
        fromDays: fromDays,
        toDays: toDays,
        syncSelect: syncSelect,
        adoptSelect: adoptSelect,
        bindSelect: bindSelect
    };
})();
