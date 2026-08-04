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
                { slug: 'overview', icon: 'bar-chart', label: 'nav.dest.overview', keywords: 'overview home stats 总览 概览 首页' },
                { slug: 'trace', icon: 'search', label: 'nav.dest.trace', keywords: 'decision trace why skipped 决策链 为什么 抑制' },
                { slug: 'cost', icon: 'dollar', label: 'nav.dest.cost', keywords: 'ai cost spend token 成本 花费' },
            ],
        },
        {
            title: 'nav.group.inbox',
            items: [
                { slug: 'alerts', icon: 'bell', label: 'nav.dest.alerts', keywords: 'alerts events inbox 告警 事件 收件箱' },
                { slug: 'work-queue', icon: 'list', label: 'nav.dest.workQueue', keywords: 'work queue triage sla 工作队列 待办' },
                { slug: 'incidents', icon: 'flame', label: 'nav.dest.incidents', keywords: 'incidents outage 事件单 故障' },
                { slug: 'investigations', icon: 'flask', label: 'nav.dest.investigations', keywords: 'deep analysis investigations 调查 深度分析' },
            ],
        },
        {
            title: 'nav.group.routing',
            items: [
                { slug: 'rules', icon: 'filter', label: 'nav.dest.rules', keywords: 'forward rules routing targets 转发 规则 路由' },
                { slug: 'silences', icon: 'volume-x', label: 'nav.dest.silences', keywords: 'silence mute maintenance 静音 屏蔽 维护窗' },
                // lowFreq: rendered in the sidebar's collapsed "rarely used"
                // tier (palette search unaffected). Seeded from the 6-week
                // adoption ledger + operator behaviour; the ww-nav-freq
                // counter recordDestination keeps will let the Aug review
                // re-tier on real numbers instead of judgement.
                { slug: 'sandbox', icon: 'zap', label: 'nav.dest.sandbox', keywords: 'sandbox test payload dry run 沙箱 测试', lowFreq: true },
                { slug: 'audit', icon: 'history', label: 'nav.dest.audit', keywords: 'audit rule history 审计 变更', lowFreq: true },
                { slug: 'ingress', icon: 'inbox', label: 'nav.dest.ingress', keywords: 'inbound setup webhook source 接入 来源' },
                { slug: 'quality', icon: 'gauge', label: 'nav.dest.quality', keywords: 'alert quality schema 质量 数据质量' },
                { slug: 'integrations', icon: 'link', label: 'nav.dest.integrations', keywords: 'integrations feishu lark 集成 飞书', lowFreq: true },
            ],
        },
        {
            title: 'nav.group.operations',
            items: [
                { slug: 'actions', icon: 'wrench', label: 'nav.dest.actions', keywords: 'action center queue todo 行动 待处理' },
                { slug: 'noise', icon: 'activity', label: 'nav.dest.noise', keywords: 'noise reduction dedup 降噪 噪音' },
                { slug: 'kb', icon: 'book-open', label: 'nav.dest.kb', keywords: 'knowledge base drafts runbook 知识库 草稿', lowFreq: true },
                { slug: 'gaps', icon: 'lightbulb', label: 'nav.dest.gaps', keywords: 'knowledge gaps missing 知识缺口', lowFreq: true },
                { slug: 'settings', icon: 'sliders', label: 'nav.dest.settings', keywords: 'runtime settings policy config 设置 配置 策略' },
            ],
        },
    ];

    const ALL = GROUPS.flatMap((group) => group.items.map((item) => ({ ...item, group: group.title })));

    let open = false;
    let matches = [];
    let cursor = 0;

    function label(item) {
        if (item.jumpLabel) return item.jumpLabel;
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
        const destinations = ALL
            .map((item) => ({ item, rank: score(item, normalized) }))
            .filter((entry) => entry.rank >= 0)
            .sort((a, b) => b.rank - a.rank)
            .map((entry) => entry.item);
        // "#245" / "245" / "告警 245" jumps straight to that record. Typing an
        // id had no answer before: the palette only knew the 19 destinations,
        // so the fastest path to alert 245 was navigate-then-scroll.
        return jumpEntries(normalized).concat(destinations);
    }

    // Synthetic entries for direct record jumps, ranked above destinations.
    function jumpEntries(normalized) {
        const idMatch = normalized.match(/(?:^|[^0-9])#?(\d{1,9})\s*$/);
        if (!idMatch) return [];
        const id = idMatch[1];
        const wantsIncident = /事件|事故|incident/.test(normalized);
        const entries = [];
        if (!wantsIncident) {
            entries.push({
                slug: 'alerts', icon: 'bell', group: 'palette.jump',
                jump: { kind: 'alert', id: id },
                jumpLabel: t('palette.jump.alert', { id: id })
            });
        }
        entries.push({
            slug: 'incidents', icon: 'flame', group: 'palette.jump',
            jump: { kind: 'incident', id: id },
            jumpLabel: t('palette.jump.incident', { id: id })
        });
        return entries;
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
                '<span class="palette-icon">' + wwIcon(item.icon) + '</span>' +
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
        close();
        // A jump is a one-off, not a place: never enters the recents list.
        if (item.jump) {
            if (item.jump.kind === 'incident' && typeof openIncident === 'function') {
                openIncident(item.jump.id);
            } else if (typeof openAlert === 'function') {
                openAlert(item.jump.id);
            }
            return;
        }
        remember(item.slug);
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
