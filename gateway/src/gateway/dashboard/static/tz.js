// FITT dashboard — localize UTC timestamps to the viewer's zone.
//
// The server renders every absolute timestamp in UTC (matching
// the logs, cron clock, and audit trail — one unambiguous source
// of truth on the wire). This script rewrites those rendered
// strings, in place, to the browser's local timezone so the
// operator reads wall-clock time without doing UTC math.
//
// Why client-side: "the client's timezone" is genuinely the
// browser's, and the browser already knows it (Intl resolves it
// with zero config). Per FITT Principle 11, auto-detect what the
// system can rather than ask the operator to declare a zone.
//
// Graceful degradation: with JS off (or this file missing from a
// stripped Docker layer), the page still shows correct UTC — the
// localization is purely additive. Nothing breaks; you just read
// UTC.
//
// Re-runs after every htmx swap (the overview / aliases / turns /
// health panels refresh on a timer via hx-trigger), so freshly
// swapped-in timestamps get localized too.

(function () {
  "use strict";

  // Matches the two server formats from dashboard/views.py:
  //   "2026-06-04 17:24:31 UTC"  (full — _fmt_iso)
  //   "17:24:31 UTC"             (time-only — generated_at_human)
  // Capture groups: date (optional), h, m, s.
  var FULL = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) UTC$/;
  var TIME = /^(\d{2}):(\d{2}):(\d{2}) UTC$/;

  // Cache formatters; building Intl.DateTimeFormat per node is
  // wasteful when a turns table has 50 rows.
  var localName = null;
  try {
    localName = Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch (e) {
    localName = null;
  }

  function two(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  // Short tz abbreviation for the suffix (e.g. "EDT"), best-effort.
  function tzAbbr(d) {
    try {
      var parts = new Intl.DateTimeFormat(undefined, {
        timeZoneName: "short",
      }).formatToParts(d);
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") {
          return parts[i].value;
        }
      }
    } catch (e) {
      /* fall through */
    }
    return "local";
  }

  // Build a UTC Date from the captured pieces. For the time-only
  // format we borrow today's UTC date — the value is a "generated
  // at" stamp from seconds ago, so the date is unambiguous.
  function toUtcDate(m, timeOnly) {
    var now = new Date();
    var y, mo, da, h, mi, s;
    if (timeOnly) {
      y = now.getUTCFullYear();
      mo = now.getUTCMonth();
      da = now.getUTCDate();
      h = parseInt(m[1], 10);
      mi = parseInt(m[2], 10);
      s = parseInt(m[3], 10);
    } else {
      y = parseInt(m[1], 10);
      mo = parseInt(m[2], 10) - 1;
      da = parseInt(m[3], 10);
      h = parseInt(m[4], 10);
      mi = parseInt(m[5], 10);
      s = parseInt(m[6], 10);
    }
    return new Date(Date.UTC(y, mo, da, h, mi, s));
  }

  function localizeFull(d) {
    return (
      d.getFullYear() +
      "-" +
      two(d.getMonth() + 1) +
      "-" +
      two(d.getDate()) +
      " " +
      two(d.getHours()) +
      ":" +
      two(d.getMinutes()) +
      ":" +
      two(d.getSeconds()) +
      " " +
      tzAbbr(d)
    );
  }

  function localizeTime(d) {
    return (
      two(d.getHours()) +
      ":" +
      two(d.getMinutes()) +
      ":" +
      two(d.getSeconds()) +
      " " +
      tzAbbr(d)
    );
  }

  // Rewrite one string if it matches a UTC format; else return null.
  function relabel(text) {
    var t = text.trim();
    var m = FULL.exec(t);
    if (m) {
      return localizeFull(toUtcDate(m, false));
    }
    m = TIME.exec(t);
    if (m) {
      return localizeTime(toUtcDate(m, true));
    }
    return null;
  }

  // Walk text nodes and title attributes under root, localizing
  // any UTC-formatted timestamp. A data-fitt-tz-done marker on the
  // owning element prevents double-processing across htmx swaps.
  function localizeTree(root) {
    if (!root || !root.querySelectorAll) {
      return;
    }

    // 1. title="..." tooltips (the turns/audit tables put the full
    //    UTC stamp here behind an "Xm ago" cell).
    var titled = root.querySelectorAll("[title]");
    for (var i = 0; i < titled.length; i++) {
      var el = titled[i];
      if (el.getAttribute("data-fitt-tz-title") === "1") {
        continue;
      }
      var rt = relabel(el.getAttribute("title") || "");
      if (rt !== null) {
        el.setAttribute("title", rt);
        el.setAttribute("data-fitt-tz-title", "1");
      }
    }

    // 2. Visible text nodes. TreeWalker so we only touch text, never
    //    element structure.
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var pending = [];
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentNode;
      if (!parent || parent.getAttribute === undefined) {
        continue;
      }
      if (parent.getAttribute("data-fitt-tz-text") === "1") {
        continue;
      }
      var replaced = relabel(node.nodeValue || "");
      if (replaced !== null) {
        pending.push([node, replaced, parent]);
      }
    }
    for (var j = 0; j < pending.length; j++) {
      pending[j][0].nodeValue = pending[j][1];
      pending[j][2].setAttribute("data-fitt-tz-text", "1");
    }
  }

  function run() {
    if (!localName) {
      return; // Intl unavailable — leave UTC as-is.
    }
    localizeTree(document.body);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }

  // htmx swaps replace panel innards on a timer; localize the
  // freshly inserted subtree. afterSettle fires once the new DOM
  // is in place.
  document.body.addEventListener("htmx:afterSettle", function (evt) {
    var target = evt && evt.detail && evt.detail.target;
    localizeTree(target || document.body);
  });
})();
