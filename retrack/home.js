// Makes the upload nicer when JavaScript is on: drag and drop, the chosen file's
// name, and results that appear below without reloading the page.
// With JavaScript off the form still works: it posts to /check and the server
// sends back the whole page with the results in it.
(function () {
  var form = document.getElementById("upload");
  var input = document.getElementById("catalog");
  var drop = document.getElementById("drop");
  var fileName = document.getElementById("file-name");
  var button = document.getElementById("check");
  var label = button.querySelector(".label");
  var results = document.getElementById("results");

  function showFile() {
    var file = input.files && input.files[0];
    fileName.textContent = file ? file.name : "";
    drop.classList.toggle("has-file", !!file);
  }
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

  // Buttons that only work with JavaScript start hidden; show them now.
  function showPrintButtons() {
    results.querySelectorAll("[data-print], .filters").forEach(function (el) { el.hidden = false; });
  }
  showPrintButtons();

  // Filter buttons: show one colour group at a time ("all" shows every group).
  function filterTo(status) {
    results.querySelectorAll("[data-filter]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.filter === status));
    });
    results.querySelectorAll(".group[data-status]").forEach(function (g) {
      g.hidden = status !== "all" && g.dataset.status !== status;
    });
  }

  // "Open all" / "Close all" for the song rows.
  function toggleAll(button) {
    var rows = results.querySelectorAll(".song");
    var open = button.textContent === "Open all";
    rows.forEach(function (row) { row.open = open; });
    button.textContent = open ? "Close all" : "Open all";
  }

  // Paper can't be clicked: open every row and show every group while printing.
  var openBeforePrint = [];
  window.addEventListener("beforeprint", function () {
    openBeforePrint = Array.prototype.filter.call(results.querySelectorAll(".song"), function (r) { return r.open; });
    results.querySelectorAll(".song").forEach(function (r) { r.open = true; });
  });
  window.addEventListener("afterprint", function () {
    results.querySelectorAll(".song").forEach(function (r) { r.open = openBeforePrint.indexOf(r) !== -1; });
  });

  function showResults(html) {
    results.innerHTML = html;
    showPrintButtons();
    var head = results.querySelector("h2");
    if (head) {
      head.setAttribute("tabindex", "-1");
      head.focus({ preventScroll: true });
    }
    results.scrollIntoView({ block: "start" });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    label.textContent = "Checking…";
    results.innerHTML = '<div class="container"><p class="checking" role="status">Checking each song with The MLC. Larger catalogs take a minute.</p></div>';

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Retrack-Fragment": "1" }
    })
      .then(function (response) { return response.text(); })
      .then(showResults)
      .catch(function () {
        showResults('<div class="container"><div class="alert" role="alert"><h2>Retrack isn’t answering</h2>' +
          '<p>The window running Retrack may have been closed. Start it again, then reload this page.</p></div></div>');
      })
      .finally(function () {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        label.textContent = "Check my catalog";
      });
  });

  results.addEventListener("click", function (e) {
    var filter = e.target.closest("[data-filter]");
    if (filter) filterTo(filter.dataset.filter);
    var expand = e.target.closest("[data-expand]");
    if (expand) toggleAll(expand);
    if (e.target.closest("[data-print]")) window.print();
  });
})();
