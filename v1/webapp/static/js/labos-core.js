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
