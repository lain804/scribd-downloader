"""Extract Scribd page metadata from its inline HTML/JavaScript."""
import json
import re
from time import perf_counter

import quickjs


MAX_PAGES = 10_000
_STUBS = r"""
var embedOptions = null;
var pages = [];
var window = globalThis;
var document = {body: {}, getElementById: function() {return null;}};
var Scribd = {
    EmbedsShow: function(selector, options) {embedOptions = options;},
    jsblock: {resolve: function() {}}, hook_tooltips: function() {}
};
var fonts = [];
var docManager = {
    addPage: function(page) {pages.push(page);}, allPagesAdded: function() {},
    addFont: function(id, shortstyle, family, fallback, weight, style) {
        fonts.push({id:id, family:family, weight:weight, style:style});
    },
    setEmbeddedDoc: function() {}, initStyles: function() {},
    setPageWidths: function() {}, setImageDomainSubstitution: function() {}
};
function DocumentManager() {return docManager;}
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
        if re.search(r"\bdocManager\.(?:addPage|addFont)\s*\(", s)
        and not s.lstrip().startswith("var DocumentManager=")
    ]
    if embed_script is None or not page_scripts:
        raise ValueError("Could not find Scribd document/page metadata; the embed may be unavailable")
    context = quickjs.Context()
    context.set_memory_limit(32 * 1024 * 1024)
    deadline = perf_counter() + 5

    def evaluate(script):
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise ValueError("Scribd metadata exceeded its execution time limit")
        context.set_time_limit(remaining)
        return context.eval(script)

    try:
        evaluate(_STUBS)
        evaluate(embed_script)
        for script in page_scripts:
            if script != embed_script:
                evaluate(script)
        data = json.loads(evaluate("""JSON.stringify({embed:embedOptions,pages:pages,
            fonts:fonts, fontHosts:docManager.fontAggregatorHosts,
            assetPrefix:docManager.assetPrefix})"""))
    except (quickjs.JSException, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not decode document metadata: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid Scribd document metadata")
    options, pages, fonts = data.get("embed"), data.get("pages"), data.get("fonts")
    document = options.get("document") if isinstance(options, dict) else None
    count = document.get("page_count") if isinstance(document, dict) else None
    if (type(count) is not int or not 1 <= count <= MAX_PAGES
            or not isinstance(pages, list) or not pages):
        raise ValueError("Document metadata has no valid page count or page descriptions")
    if not isinstance(fonts, list):
        raise ValueError("Invalid Scribd font metadata")
    if fonts:
        options["font_data"] = {"fonts": fonts, "hosts": data.get("fontHosts"),
                                "asset_prefix": data.get("assetPrefix")}
    return options, pages
