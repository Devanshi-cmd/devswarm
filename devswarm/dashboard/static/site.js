// Light by default. The header's switch slides to night mode and remembers the choice.
(function () {
  var root = document.documentElement;
  try {
    if (localStorage.getItem("theme") === "dark") root.dataset.theme = "dark";
  } catch (error) {}

  function showTheme() {
    var toggle = document.querySelector(".theme-switch");
    if (toggle) toggle.setAttribute("aria-checked", String(root.dataset.theme === "dark"));
  }

  document.addEventListener("click", function (event) {
    var faq = document.querySelector(".faq[open]");
    if (faq && !faq.contains(event.target)) faq.open = false;

    if (!event.target.closest(".theme-switch")) return;
    var next = root.dataset.theme === "dark" ? "light" : "dark";
    if (next === "dark") root.dataset.theme = "dark";
    else delete root.dataset.theme;
    showTheme();
    try { localStorage.setItem("theme", next); } catch (error) {}
  });

  document.addEventListener("keydown", function (event) {
    var faq = document.querySelector(".faq[open]");
    if (event.key === "Escape" && faq) {
      faq.open = false;
      faq.querySelector("summary").focus();
    }
  });

  document.addEventListener("DOMContentLoaded", showTheme);
})();
