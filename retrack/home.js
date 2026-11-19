// Small extras for the Retrack pages. Every page works without this script:
// the forms post normally, the progress page refreshes itself, and the privacy
// panel and fix-pack total are filled in by the server. This only makes them
// nicer: drag and drop, live progress, a live total and live privacy text.
//
// No rules live here. The progress text, the price lines and the privacy text
// all come from the server; the script only picks which one to show.
(function () {
  // ---- Upload: drag and drop, and the chosen file's name ----
  var drop = document.getElementById("drop");
  var input = document.getElementById("catalog");
  var fileName = document.getElementById("file-name");
  if (drop && input) {
    var showFile = function () {
      var file = input.files && input.files[0];
      fileName.textContent = file ? file.name : "";
      drop.classList.toggle("has-file", !!file);
    };
    input.addEventListener("change", showFile);
    ["dragenter", "dragover"].forEach(function (type) {
      drop.addEventListener(type, function (e) { e.preventDefault(); drop.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (type) {
      drop.addEventListener(type, function () { drop.classList.remove("is-over"); });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        showFile();
      }
    });
  }

  // ---- Pay step: show the privacy text that matches the two extra checks ----
  // (CSS does this too where the browser supports :has; this covers the rest.)
  var smart = document.getElementById("smart_names");
  var ai = document.getElementById("ai_search");
  if (smart && ai) {
    var showPrivacy = function () {
      var key = (smart.checked ? "1" : "0") + (ai.checked ? "1" : "0");
      document.querySelectorAll("[data-privacy]").forEach(function (list) {
        list.hidden = list.getAttribute("data-privacy") !== key;
      });
    };
    smart.addEventListener("change", showPrivacy);
    ai.addEventListener("change", showPrivacy);
    showPrivacy();
  }

  // ---- Progress page: ask the server how far the check is, every second ----
  var progressTitle = document.querySelector("[data-status-url]");
  if (progressTitle) {
    var bar = document.getElementById("progress-bar");
    var refresh = document.querySelector('meta[http-equiv="refresh"]');
    if (refresh) refresh.remove(); // the script takes over from the meta refresh
    var poll = function () {
      fetch(progressTitle.getAttribute("data-status-url"), { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.state !== "running") { window.location.href = s.next; return; }
          progressTitle.textContent = s.text;
          bar.max = Math.max(s.total, 1);
          bar.value = s.done;
          bar.textContent = s.done + " of " + s.total;
          setTimeout(poll, 1000);
        })
        .catch(function () {
          progressTitle.textContent = "Retrack isn’t answering. If you closed its window, start it again and upload your file.";
        });
    };
    setTimeout(poll, 1000);
  }

  // ---- Results and fix page: tabs, open all, print ----
  var results = document.querySelector(".results");
  if (results) {
    results.querySelectorAll("[data-print], .filters").forEach(function (el) { el.hidden = false; });

    var filterTo = function (status) {
      results.querySelectorAll("[data-filter]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.filter === status));
      });
      results.querySelectorAll(".group[data-status]").forEach(function (g) {
        g.hidden = status !== "all" && g.dataset.status !== status;
      });
      // "Summary" shows the summary of findings above every song; a colour shows just its songs.
      results.querySelectorAll("[data-summary]").forEach(function (panel) {
        panel.hidden = status !== "all";
      });
    };
    var toggleAll = function (button) {
      var open = button.textContent === "Open all";
      results.querySelectorAll(".song").forEach(function (row) { row.open = open; });
      button.textContent = open ? "Close all" : "Open all";
    };
    results.addEventListener("click", function (e) {
      var filter = e.target.closest("[data-filter]");
      if (filter) filterTo(filter.dataset.filter);
      var expand = e.target.closest("[data-expand]");
      if (expand) toggleAll(expand);
      if (e.target.closest("[data-print]")) window.print();
    });

    // Paper can't be clicked: open every row (and every "More detail") while printing.
    var openBefore = [];
    var openable = ".song, .more";
    window.addEventListener("beforeprint", function () {
      openBefore = Array.prototype.filter.call(results.querySelectorAll(openable), function (r) { return r.open; });
      results.querySelectorAll(openable).forEach(function (r) { r.open = true; });
    });
    window.addEventListener("afterprint", function () {
      results.querySelectorAll(openable).forEach(function (r) { r.open = openBefore.indexOf(r) !== -1; });
    });
  }

  // ---- Fix page: ask before leaving ----
  // (Its tabs, Open all and Print are handled by the results code above: same markup.)
  // The browser shows its own "Leave this page?" message; scripts can't change its words.
  // Downloads, printing and the template form don't count as leaving.
  var fixes = document.querySelector("[data-warn-leave]");
  if (fixes) {
    // Keep "About 47 minutes left." current. The server writes the words; this only asks again.
    fixes.querySelectorAll("[data-time-left-url]").forEach(function (el) {
      var refresh = function () {
        fetch(el.getAttribute("data-time-left-url"), { cache: "no-store" })
          .then(function (r) {
            if (!r.ok) { leavingOnPurpose = true; window.location.reload(); return null; } // expired: show why
            return r.json();
          })
          .then(function (t) { if (t) el.textContent = t.text; })
          .catch(function () {});
      };
      setInterval(refresh, 30000);
    });
    var leavingOnPurpose = false;
    fixes.addEventListener("click", function (e) {
      if (e.target.closest("[data-no-warn]")) {
        leavingOnPurpose = true;
        setTimeout(function () { leavingOnPurpose = false; }, 1000);
      }
    });
    fixes.addEventListener("submit", function () { leavingOnPurpose = true; });
    window.addEventListener("beforeunload", function (e) {
      if (leavingOnPurpose) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

  // ---- Fix pack: "N songs × $X" as songs are ticked ----
  // The server sends every possible line (0 songs, 1 song, ...); we pick one.
  var fixForm = document.getElementById("fix-form");
  if (fixForm) {
    var lines = JSON.parse(fixForm.getAttribute("data-lines"));
    var total = document.getElementById("fix-total");
    var update = function () {
      var n = fixForm.querySelectorAll('input[name="song"]:checked').length;
      total.textContent = lines[n] + ".";
    };
    fixForm.addEventListener("change", update);
    update();
  }
})();
