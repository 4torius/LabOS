// Shared parameter-form rendering logic used by both dashboard.html and
// workflows.html, so a fix here never needs to be duplicated in two templates
// again (see commit history: the Constrained/Set default-selection bug had to
// be fixed twice in one night because this used to be copy-pasted).

// Build the <select> HTML for a SiLA2 Constrained/Set parameter with a
// server-provided options list. Required params must never submit '' to the
// server (violates the Constrained/Set), so the blank placeholder is skipped
// and the first option is preselected unless currentValue matches one exactly.
function buildConstrainedSelectHtml(paramName, options, currentValue, required, selectClass, humanize) {
    var hasMatchingValue = options.indexOf(currentValue) !== -1;
    var html = '<select class="' + (selectClass || 'form-select param-input') + '" data-param="' + paramName + '">';
    if (!required || !hasMatchingValue) {
        html += '<option value="">-- Select --</option>';
    }
    for (var i = 0; i < options.length; i++) {
        var opt = options[i];
        var isSelected = hasMatchingValue ? (opt === currentValue) : (required && i === 0);
        var label = humanize ? opt.replace(/_/g, ' ') : opt;
        html += '<option value="' + opt + '"' + (isSelected ? ' selected' : '') + '>' + label + '</option>';
    }
    return html + '</select>';
}

// Pre-populate a workflow step's params object with the same default that
// buildConstrainedSelectHtml() would visually preselect for any required
// Constrained/Set parameter that isn't already set. Without this, a dropdown
// left untouched shows a value visually but never gets written into
// step.params (that only happens via the <select>'s 'change' event), so the
// step would submit with the parameter silently missing.
function applyRequiredConstrainedDefaults(step, cmd) {
    if (!cmd || !cmd.parameters) return;
    cmd.parameters.forEach(function(p) {
        if (p.required && p.options && p.options.length > 0 && !step.params[p.name]) {
            var hasMatchingDefault = p.options.indexOf(p.default) !== -1;
            step.params[p.name] = hasMatchingDefault ? p.default : p.options[0];
        }
    });
}

// Same gap as applyRequiredConstrainedDefaults(), for required non-Constrained
// String parameters (e.g. Tecan Connect's ConnectionString, documented as
// "empty string uses USB default"): the <input> shows '' but that never
// reaches step.params without a 'change'/'input' event, so an untouched field
// silently vanishes from validation/execution. Empty string is an explicitly
// valid value for String params (see workflow.py's validator), unlike
// Constrained/Set params which must never receive '' -- hence the separate,
// stricter handling above for those.
function applyRequiredStringDefaults(step, cmd) {
    if (!cmd || !cmd.parameters) return;
    cmd.parameters.forEach(function(p) {
        var isConstrained = p.options && p.options.length > 0;
        if (p.required && !isConstrained && p.type === 'String' && !step.params.hasOwnProperty(p.name)) {
            step.params[p.name] = '';
        }
    });
}
