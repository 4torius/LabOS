/*!
 * labos-params.js — rendering a SiLA2 command's parameters as a form.
 *
 * Supersedes param-form-shared.js, which existed because the same select-
 * building logic had been copy-pasted into dashboard.html and workflows.html
 * and the Constrained/Set default-selection bug had to be fixed twice in one
 * night. This carries that logic forward and adds the piece it was missing:
 * a value and the label shown for it are not always the same string.
 *
 * A deck slot ("C2") explains itself. "Source_1" does not — an operator
 * choosing what the gripper should move needs to see which plate that is and
 * where it currently sits, while the command still receives "Source_1".
 * The server sends both, as `options_detail`: [{value, label}].
 */
(function (global) {
    'use strict';

    var LabOS = global.LabOS = global.LabOS || {};
    var esc = LabOS.escapeAttr || function (v) { return String(v === null || v === undefined ? '' : v); };
    var escText = LabOS.escapeHtml || esc;

    /**
     * Normalise a parameter's choices into [{value, label}].
     *
     * Accepts `options_detail` (preferred), a plain `options` array of
     * strings, or an array of {value,label}/{id,name} objects, because the
     * dynamic-options endpoint and the FDL constraints have never agreed on a
     * shape and there is no reason a caller should have to care.
     */
    function normalizeOptions(param) {
        if (!param) return [];
        var detail = param.options_detail;
        if (detail && detail.length) {
            return detail.map(function (o) {
                return { value: o.value, label: o.label || o.value };
            });
        }
        return (param.options || []).map(function (o) {
            if (o && typeof o === 'object') {
                var value = o.value !== undefined ? o.value : o.id;
                return { value: value, label: o.label || o.name || value };
            }
            return { value: o, label: o };
        });
    }

    /**
     * Build the <select> for a parameter with a fixed set of choices.
     *
     * A required parameter must never submit '' (it violates the SiLA2
     * Constrained/Set), so the blank placeholder is skipped and the first
     * option is preselected unless the current value matches one exactly.
     *
     * @param {string} paramName
     * @param {object|Array} paramOrOptions  a parameter object, or a bare options array
     * @param {*} currentValue
     * @param {boolean} required
     * @param {string} [selectClass]
     * @param {boolean} [humanize]  replace underscores in the label
     */
    function buildSelect(paramName, paramOrOptions, currentValue, required, selectClass, humanize) {
        var options = Array.isArray(paramOrOptions)
            ? normalizeOptions({ options: paramOrOptions })
            : normalizeOptions(paramOrOptions);

        var values = options.map(function (o) { return o.value; });
        var hasMatch = values.indexOf(currentValue) !== -1;

        var html = '<select class="' + (selectClass || 'form-select param-input') +
                   '" data-param="' + esc(paramName) + '">';
        if (!required || !hasMatch) {
            html += '<option value="">-- Select --</option>';
        }
        for (var i = 0; i < options.length; i++) {
            var selected = hasMatch ? (options[i].value === currentValue) : (required && i === 0);
            var label = options[i].label;
            if (humanize && label === options[i].value) label = String(label).replace(/_/g, ' ');
            html += '<option value="' + esc(options[i].value) + '"' +
                    (selected ? ' selected' : '') + '>' + escText(label) + '</option>';
        }
        html += '</select>';

        if (!options.length) {
            // An empty picker with no explanation reads as a broken page. It is
            // usually correct — there is genuinely nothing to choose — and the
            // operator needs to know why.
            html += '<div class="form-text text-warning">' +
                    'Nothing to choose here yet. If this is a deck position, ' +
                    'check that a hardware config is active on the Hardware page.' +
                    '</div>';
        }
        return html;
    }

    /**
     * Pre-populate a step's params with the default a required select shows.
     *
     * Without this, a dropdown left untouched displays a value that never gets
     * written into the step (that only happens on the select's 'change'
     * event), so the step is submitted with the parameter silently missing.
     */
    function applyRequiredSelectDefaults(step, cmd) {
        if (!cmd || !cmd.parameters) return;
        cmd.parameters.forEach(function (p) {
            var options = normalizeOptions(p);
            if (!p.required || !options.length || step.params[p.name]) return;
            var values = options.map(function (o) { return o.value; });
            step.params[p.name] = values.indexOf(p.default) !== -1 ? p.default : values[0];
        });
    }

    /**
     * The same gap for required non-constrained String parameters.
     *
     * Empty string is an explicitly valid value for those (Tecan's
     * ConnectionString documents "" as "use the default USB connection"),
     * unlike a Constrained/Set parameter which must never receive it — hence
     * the separate, stricter handling above.
     */
    function applyRequiredStringDefaults(step, cmd) {
        if (!cmd || !cmd.parameters) return;
        cmd.parameters.forEach(function (p) {
            var constrained = normalizeOptions(p).length > 0;
            if (p.required && !constrained && p.type === 'String' &&
                !Object.prototype.hasOwnProperty.call(step.params, p.name)) {
                step.params[p.name] = '';
            }
        });
    }

    /**
     * A form field hands back a string. SiLA2 wants the declared type.
     *
     * Every HTML control produces text, so a Boolean parameter filled in from
     * a form arrives as "false" - a non-empty string, which is emphatically
     * not false - and the sila2 client refuses it with "Expected a bool
     * value". The workflow builder had no Boolean field at all, so
     * `SetModuleLatch` went out with `Open: "false"` and `MoveLabware` with
     * `UseGripper: "UseGripper"`, the field's own name typed into a free-text
     * box. Both failed four times and stopped the run, after the Tecan had
     * already moved the plate.
     *
     * Numbers have the same shape of fault waiting: an Integer parameter
     * submitted as "200" is a string too.
     *
     * Applied on the way out, so a workflow saved before this existed - with
     * "false" already written into its JSON - is fixed when it runs, without
     * anyone reopening it.
     */
    function coerce(value, declaredType) {
        var type = String(declaredType || '').toLowerCase();

        if (type === 'boolean') {
            if (typeof value === 'boolean') return value;
            if (value === null || value === undefined) return false;
            var text = String(value).trim().toLowerCase();
            // Anything that is not an affirmative is false, including the
            // parameter's own name and an empty box.
            return text === 'true' || text === '1' || text === 'yes' ||
                   text === 'on' || text === 'si' || text === 'sì';
        }

        if (type === 'integer') {
            if (value === '' || value === null || value === undefined) return value;
            var whole = parseInt(value, 10);
            return isNaN(whole) ? value : whole;
        }

        if (type === 'real' || type === 'float' || type === 'double' || type === 'number') {
            if (value === '' || value === null || value === undefined) return value;
            var real = parseFloat(value);
            return isNaN(real) ? value : real;
        }

        return value;
    }

    /**
     * Coerce every parameter of one step against the command's own metadata.
     * Unknown parameters are passed through untouched - `hal_config` and the
     * other workflow-only keys are not the command's business.
     */
    function coerceAll(params, cmd) {
        if (!cmd || !cmd.parameters) return params;
        var out = {};
        Object.keys(params || {}).forEach(function (key) { out[key] = params[key]; });
        cmd.parameters.forEach(function (p) {
            if (Object.prototype.hasOwnProperty.call(out, p.name)) {
                out[p.name] = coerce(out[p.name], p.type);
            }
        });
        return out;
    }

    /** Required Boolean parameters need a value; nothing else supplies one. */
    function applyRequiredBooleanDefaults(step, cmd) {
        if (!cmd || !cmd.parameters) return;
        cmd.parameters.forEach(function (p) {
            if (String(p.type || '').toLowerCase() !== 'boolean') return;
            if (!p.required) return;
            if (Object.prototype.hasOwnProperty.call(step.params, p.name)) return;
            // false is the conservative one for both booleans on this surface:
            // UseGripper false pauses for a manual move, Open false keeps the
            // latch shut.
            step.params[p.name] = coerce(p.default, 'boolean');
        });
    }

    /** The two choices, as a <select> that submits a real boolean. */
    function buildBooleanSelect(paramName, currentValue, selectClass) {
        var on = coerce(currentValue, 'boolean');
        return '<select class="' + esc(selectClass || 'form-select form-select-sm') +
               '" data-param="' + esc(paramName) + '" data-type="Boolean">' +
               '<option value="true"' + (on ? ' selected' : '') + '>true</option>' +
               '<option value="false"' + (on ? '' : ' selected') + '>false</option>' +
               '</select>';
    }

    /** True when a command is documented as physically moving the instrument. */
    function movesHardware(cmd) {
        return !!(cmd && cmd.description &&
                  cmd.description.indexOf('MOVES HARDWARE') !== -1);
    }

    LabOS.params = {
        normalizeOptions: normalizeOptions,
        buildSelect: buildSelect,
        buildBooleanSelect: buildBooleanSelect,
        applyRequiredSelectDefaults: applyRequiredSelectDefaults,
        applyRequiredStringDefaults: applyRequiredStringDefaults,
        applyRequiredBooleanDefaults: applyRequiredBooleanDefaults,
        coerce: coerce,
        coerceAll: coerceAll,
        movesHardware: movesHardware
    };

    // ── Backwards-compatible aliases ────────────────────────────────────────
    // The templates still call these by their old names in a few places; they
    // are the same functions, so both spellings stay correct.
    global.buildConstrainedSelectHtml = function (paramName, options, currentValue, required, selectClass, humanize) {
        return buildSelect(paramName, options, currentValue, required, selectClass, humanize);
    };
    global.applyRequiredConstrainedDefaults = applyRequiredSelectDefaults;
    global.applyRequiredStringDefaults = applyRequiredStringDefaults;

})(window);
