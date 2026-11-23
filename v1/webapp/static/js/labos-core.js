/*!
 * labos-core.js — the helpers every page needs.
 *
 * These used to be copy-pasted into each template's inline <script>, in
 * slightly different versions: three spellings of "escape this before putting
 * it in HTML", two date formatters, and a toast that only existed on the pages
 * whose author remembered to paste it. A fix had to be applied N times and
 * usually was not.
 *
 * Everything hangs off one global, `LabOS`, so a page can tell at a glance
 * what is shared and what is its own.
 */
(function (global) {
    'use strict';

    var LabOS = global.LabOS = global.LabOS || {};

    // ── Escaping ────────────────────────────────────────────────────────────
    // The templates build HTML by string concatenation, so anything that came
    // from a server, a file name or an operator has to be escaped on the way
    // in. Two functions because the rules differ: inside an attribute, quotes
    // matter; inside text, they do not.

    LabOS.escapeHtml = function (value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    };

    LabOS.escapeAttr = function (value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    };

    // ── How an instrument looks ─────────────────────────────────────────────
    // The server says, in its catalog (`display`: a Bootstrap icon and a
    // contextual colour). Both pages used to carry a table keyed on whether
    // the name contained "tecan", "opentrons", "mobile" or "manual", so every
    // other instrument was a grey gear, and the workflow builder filed every
    // one of them under the same key - two new instruments overwrote each
    // other in the toolbox. A server that declares nothing gets a look from
    // its SiLA2 server type, which it also reports itself.

    var COLORS = ['primary', 'secondary', 'success', 'danger', 'warning', 'info', 'dark'];
    var BY_SERVER_TYPE = [
        [/liquid|pipett|dispens/i, 'bi-droplet-fill', 'primary'],
        [/reader|spectro|photomet|imag|camera|microscop/i, 'bi-eye-fill', 'success'],
        [/operator|manual|human/i, 'bi-hand-index-thumb', 'info'],
        [/robot|mobile|arm|manipulat|transport/i, 'bi-robot', 'warning'],
        [/incubat|heat|thermo|temperat/i, 'bi-thermometer-half', 'danger']
    ];

    LabOS.instrumentStyle = function (instrument) {
        var declared = (instrument && instrument.display) || {};
        var icon = /^bi-[a-z0-9-]+$/.test(declared.icon || '') ? declared.icon : null;
        var color = COLORS.indexOf(declared.color) !== -1 ? declared.color : null;
        if (!icon || !color) {
            var type = (instrument && instrument.type) || '';
            for (var i = 0; i < BY_SERVER_TYPE.length; i++) {
                if (BY_SERVER_TYPE[i][0].test(type)) {
                    icon = icon || BY_SERVER_TYPE[i][1];
                    color = color || BY_SERVER_TYPE[i][2];
                    break;
                }
            }
        }
        color = color || 'secondary';
        return {
            icon: icon || 'bi-cpu',
            color: color,
            bg: color === 'warning' || color === 'info' ? 'bg-' + color + ' text-dark' : 'bg-' + color
        };
    };

    // ── HTTP ────────────────────────────────────────────────────────────────

    /**
     * fetch + JSON + a readable error.
     *
     * The bare `fetch(...).then(r => r.json())` scattered through the templates
     * turns a 500 into "Unexpected token < in JSON", which tells the operator
     * nothing. This surfaces the status and the server's own message.
     */
    LabOS.fetchJson = function (url, options) {
        return fetch(url, options).then(function (response) {
            var contentType = response.headers.get('content-type') || '';
            var parse = contentType.indexOf('application/json') !== -1
                ? response.json()
                : response.text().then(function (text) { return { _text: text }; });

            return parse.then(function (body) {
                if (response.ok) return body;
                var detail = body && (body.error || body.detail || body.message || body._text);
                var error = new Error(detail
                    ? String(detail).slice(0, 400)
                    : (response.status + ' ' + response.statusText));
                error.status = response.status;
                error.body = body;
                throw error;
            });
        });
    };

    LabOS.postJson = function (url, payload) {
        return LabOS.fetchJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        });
    };

    // ── Formatting ──────────────────────────────────────────────────────────

    LabOS.formatDate = function (value) {
        if (!value) return '';
        var text = String(value);
        // ISO timestamps are what every endpoint returns; show them without
        // the T and without microseconds, which no operator reads.
        return text.replace('T', ' ').slice(0, 19);
    };

    LabOS.formatDuration = function (seconds) {
        if (seconds === null || seconds === undefined || isNaN(seconds)) return '';
        var total = Math.round(Number(seconds));
        if (total < 60) return Number(seconds).toFixed(1) + 's';
        if (total < 3600) return Math.floor(total / 60) + 'm ' + (total % 60) + 's';
        return Math.floor(total / 3600) + 'h ' + Math.floor((total % 3600) / 60) + 'm';
    };

    LabOS.formatBytes = function (bytes) {
        if (!bytes) return '0 KB';
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    };

    /**
     * Show the interesting part of a value that is usually JSON in a string.
     * Most SiLA2 properties are shaped that way, and a card has no room for
     * a wall of braces.
     */
    LabOS.summarizeValue = function (value) {
        if (value === null || value === undefined || value === '') return '—';
        if (typeof value === 'string') {
            var trimmed = value.trim();
            if (trimmed.charAt(0) !== '{' && trimmed.charAt(0) !== '[') return trimmed;
            try { value = JSON.parse(trimmed); } catch (e) { return trimmed; }
        }
        if (Array.isArray(value)) return value.length ? value.length + ' item(s)' : 'none';
        if (typeof value === 'object') {
            var preferred = ['state', 'name', 'status', 'value'];
            for (var i = 0; i < preferred.length; i++) {
                if (value[preferred[i]] !== undefined) return String(value[preferred[i]]);
            }
            var keys = Object.keys(value);
            return keys.length ? keys.length + ' field(s)' : '—';
        }
        return String(value);
    };

    // ── Toasts ──────────────────────────────────────────────────────────────

    var TOAST_STYLES = {
        success: { bg: 'bg-success', text: 'text-white' },
        error: { bg: 'bg-danger', text: 'text-white' },
        danger: { bg: 'bg-danger', text: 'text-white' },
        warning: { bg: 'bg-warning', text: '' },
        info: { bg: 'bg-info', text: 'text-white' }
    };

    /**
     * @param {string} type    success | error | warning | info
     * @param {string} title
     * @param {string} message
     * @param {object} [opts]  {sticky: true} for something the operator must act on
     */
    LabOS.toast = function (type, title, message, opts) {
        var settings = opts || {};
        var style = TOAST_STYLES[type] || TOAST_STYLES.info;
        var toast = document.createElement('div');
        toast.className = 'toast show position-fixed labos-toast';
        toast.style.cssText = 'top: 70px; right: 20px; z-index: 9999; min-width: 270px; max-width: 420px;';

        // Stack, rather than pile up in the same spot: several failures at once
        // used to render exactly on top of each other.
        var existing = document.querySelectorAll('.labos-toast').length;
        toast.style.top = (70 + existing * 84) + 'px';

        toast.innerHTML =
            '<div class="toast-header ' + style.bg + ' ' + style.text + '">' +
            '<strong class="me-auto">' + LabOS.escapeHtml(title) + '</strong>' +
            '<button type="button" class="btn-close btn-close-white" ' +
            'onclick="this.closest(\'.toast\').remove()"></button></div>' +
            '<div class="toast-body" style="white-space:pre-line">' +
            LabOS.escapeHtml(message) + '</div>';

        document.body.appendChild(toast);
        // A sticky toast is for something that needs a decision — an instrument
        // that did not confirm a stop, say. Those must not vanish on their own.
        if (!settings.sticky) {
            setTimeout(function () { toast.remove(); }, settings.duration || 5000);
        }
        return toast;
    };


    // ── Workflow step dependencies ──────────────────────────────────────────
    // A step's `dependsOn` holds 1-based POSITIONS, not stable ids, and the
    // builder lets steps be reordered and deleted by splicing the array. Those
    // two facts used to be left to disagree: deleting step 1 turned "depends on
    // 2" into a reference to what had been step 3.
    //
    // That was cosmetic while nothing read the dependencies. It is not any
    // more — DependsOn is what orders execution, so a stale number silently
    // reorders or drops physical steps. These two helpers keep the numbers
    // pointing at the steps the author picked, and they are pure so they can
    // be tested.

    /**
     * Dependencies after the step at `removedIndex` (0-based) is deleted.
     *
     * Whatever depended on the removed step inherits what *it* depended on, so
     * deleting the middle of 1→2→3 leaves 1→3 rather than leaving 3 with no
     * predecessor. Simply dropping the reference would have been more literal
     * and worse: in a sequential workflow it removes an ordering constraint, so
     * deleting one step would quietly make two physical steps eligible to run
     * at the same time. This is the ordinary way to delete a node from a DAG —
     * close over it, preserving reachability.
     *
     * @returns {Array<Array<number>>} one dependency list per surviving step
     */
    LabOS.dependenciesAfterRemove = function (steps, removedIndex) {
        var removedNumber = removedIndex + 1;
        var inherited = (steps[removedIndex] && steps[removedIndex].dependsOn) || [];
        var shift = function (n) { return n > removedNumber ? n - 1 : n; };

        return steps
            .filter(function (_s, i) { return i !== removedIndex; })
            .map(function (step, newIndex) {
                var deps = step.dependsOn || [];
                var kept = deps.filter(function (n) { return n !== removedNumber; }).map(shift);
                if (deps.indexOf(removedNumber) !== -1) {
                    inherited.map(shift).forEach(function (n) {
                        // Never let a step inherit a reference to itself: that
                        // happens when the removed step depended on this one.
                        if (n !== newIndex + 1 && kept.indexOf(n) === -1) kept.push(n);
                    });
                }
                return kept.sort(function (a, b) { return a - b; });
            });
    };

    /**
     * Dependencies after the step at `from` (0-based) moves to `to`.
     * Every reference follows the step it pointed at, so the graph the author
     * drew survives a reorder even when it now runs in a different order.
     * @returns {Array<Array<number>>} one dependency list per step, in the new order
     */
    LabOS.dependenciesAfterMove = function (steps, from, to) {
        var order = steps.map(function (_s, i) { return i; });
        order.splice(to, 0, order.splice(from, 1)[0]);
        // old 1-based number -> new 1-based number
        var renumbered = {};
        order.forEach(function (oldIndex, newIndex) {
            renumbered[oldIndex + 1] = newIndex + 1;
        });
        return order.map(function (oldIndex) {
            return (steps[oldIndex].dependsOn || []).map(function (n) {
                return renumbered[n] || n;
            });
        });
    };

    /**
     * Dependencies of the existing steps after `count` steps are inserted
     * below the first `at` steps (so the new ones take positions at+1..at+count).
     *
     * Every reference to a step that moved down follows it. The step that was
     * just below the insertion point and waited for the step just above it now
     * waits for the last inserted step instead: inserting a block into a chain
     * puts it *in* the chain (selected -> block -> next) rather than beside it.
     * @returns {Array<Array<number>>} one dependency list per existing step, in order
     */
    LabOS.dependenciesAfterInsert = function (steps, at, count) {
        return steps.map(function (step, i) {
            var deps = (step.dependsOn || []).map(function (n) { return n > at ? n + count : n; });
            if (i === at && at > 0 && count > 0) {
                var k = deps.indexOf(at);
                if (k !== -1) deps[k] = at + count;
            }
            return deps.sort(function (a, b) { return a - b; });
        });
    };

    // ── Choosing what a step waits for ──────────────────────────────────────
    // Ticking boxes in a list of every step is how DependsOn used to be set,
    // and nearly every real case is one of four shapes. Each is one click:
    //
    //   after  — waits for the step written just before it (the default)
    //   with   — waits for what the previous step waits for, so it runs
    //            alongside it: the branch of a fork
    //   join   — waits for every open branch above it: where two plates meet
    //   start  — waits for nothing
    //
    // Anything else is "custom", chosen by clicking steps in the list.

    function _sorted(list) {
        return (list || []).slice().sort(function (a, b) { return a - b; });
    }
    function _same(a, b) {
        a = _sorted(a); b = _sorted(b);
        return a.length === b.length && a.every(function (n, i) { return n === b[i]; });
    }

    /**
     * Positions (1-based) above `index` that nothing else above it waits for:
     * the ends of the branches still open at that point.
     */
    LabOS.openBranches = function (steps, index) {
        var waitedFor = {};
        for (var i = 0; i < index; i++) {
            (steps[i].dependsOn || []).forEach(function (n) { waitedFor[n] = true; });
        }
        var open = [];
        for (var p = 1; p <= index; p++) if (!waitedFor[p]) open.push(p);
        return open;
    };

    /** The dependency list a mode gives the step at `index` (0-based). */
    LabOS.dependenciesFor = function (steps, index, mode) {
        if (index <= 0 || mode === 'start') return [];
        if (mode === 'after') return [index];
        if (mode === 'with') return _sorted(steps[index - 1].dependsOn);
        if (mode === 'join') return LabOS.openBranches(steps, index);
        return _sorted(steps[index].dependsOn);
    };

    /** Which of the four shapes a step's dependencies are, or 'custom'. */
    LabOS.dependencyMode = function (steps, index) {
        var deps = steps[index].dependsOn || [];
        if (index === 0) return deps.length ? 'custom' : 'start';
        var order = ['after', 'with', 'join', 'start'];
        for (var i = 0; i < order.length; i++) {
            var candidate = LabOS.dependenciesFor(steps, index, order[i]);
            // "with" a step that itself starts at the beginning is just "start".
            if (order[i] === 'with' && candidate.length === 0) continue;
            if (_same(deps, candidate)) return order[i];
        }
        return 'custom';
    };

    // ── Page log ────────────────────────────────────────────────────────────
    // Several pages have a scrolling activity log with the same shape.

    LabOS.log = function (message, level) {
        var target = document.getElementById('activity-log') || document.getElementById('log');
        if (!target) return;
        var colours = { success: 'text-success', error: 'text-danger', warning: 'text-warning' };
        var line = document.createElement('div');
        line.className = colours[level] || 'text-muted';
        line.textContent = '[' + new Date().toLocaleTimeString() + '] ' + message;
        target.insertBefore(line, target.firstChild);
        while (target.childElementCount > 200) target.removeChild(target.lastChild);
    };

})(window);
