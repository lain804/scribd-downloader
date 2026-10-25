"""Extract Scribd page metadata from its inline HTML/JavaScript."""
import json
import re

import quickjs


_STUBS = r"""
var embedOptions = null;
var pages = [];
var window = globalThis;
var document = {body: {}, getElementById: function() {return null;}};
var Scribd = {
    EmbedsShow: function(selector, options) {embedOptions = options;},
    jsblock: {resolve: function() {}}, hook_tooltips: function() {}
};
var docManager = {addPage: function(page) {pages.push(page);}, allPagesAdded: function() {}};
function $() {return {addClass: function() {}};}
function trackEvent() {}
function XMLHttpRequest() {}
XMLHttpRequest.prototype.open = function() {};
XMLHttpRequest.prototype.send = function() {};
"""


def extract_embed_data(root):
    scripts = [node.text or "" for node in root.xpath("//script[not(@src)]")]
    embed_script = next((s for s in scripts if re.search(r"new\s+Scribd\.EmbedsShow\s*\(", s)), None)
    page_scripts = [
        s for s in scripts
        if re.search(r"\bdocManager\.addPage\s*\(", s)
        and not s.lstrip().startswith("var DocumentManager=")
    ]
    if embed_script is None or not page_scripts:
        raise ValueError("Could not find Scribd document/page metadata; the embed may be unavailable")
    context = quickjs.Context()
    context.set_memory_limit(32 * 1024 * 1024)
    context.set_time_limit(5)
    try:
        context.eval(_STUBS)
        context.eval(embed_script)
        for script in page_scripts:
            if script != embed_script:
                context.eval(script)
        data = json.loads(context.eval("JSON.stringify({embed:embedOptions,pages:pages})"))
    except quickjs.JSException as exc:
        raise ValueError(f"Could not decode document metadata: {exc}") from exc
    options, pages = data["embed"], data["pages"]
    document = options.get("document", {}) if isinstance(options, dict) else {}
    count = document.get("page_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or not pages:
        raise ValueError("Document metadata has no valid page count or page descriptions")
    return options, pages
