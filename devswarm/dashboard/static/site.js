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

// "How it works?" walkthrough. CSS draws and times every step; this only picks the step.
// Each dot's fill animation lasts as long as its step, and its end moves to the next one.
(function () {
  document.documentElement.classList.add("js");
  var STATE = ["at", "locked", "tests", "loop", "done"];

  function setup() {
    var root = document.querySelector(".hiw");
    if (!root) return;
    var stage = root.querySelector(".hiw-stage");
    var nodes = root.querySelectorAll(".hiw-node");
    var captions = root.querySelectorAll(".hiw-caption");
    var dots = root.querySelectorAll(".hiw-dot");
    var play = root.querySelector(".hiw-play");
    var current = 0;
    var paused = false;
    var hidden = true;

    function show(index) {
      current = index;
      var data = captions[index].dataset;
      STATE.forEach(function (name) {
        if (data[name]) stage.dataset[name] = data[name];
        else delete stage.dataset[name];
      });
      for (var i = 0; i < captions.length; i++) {
        captions[i].classList.toggle("is-current", i === index);
        dots[i].classList.toggle("is-current", i === index);
      }
      for (var j = 0; j < nodes.length; j++) {
        nodes[j].classList.toggle("is-active", nodes[j].dataset.node === data.at);
      }
    }

    function updatePaused() {
      root.classList.toggle("is-paused", paused || hidden);
      play.setAttribute("aria-pressed", String(paused));
      play.setAttribute("aria-label", paused ? "Play the walkthrough" : "Pause the walkthrough");
    }

    dots.forEach(function (dot, index) {
      dot.addEventListener("click", function () { show(index); });
    });
    play.addEventListener("click", function () {
      paused = !paused;
      updatePaused();
    });
    root.addEventListener("animationend", function (event) {
      if (event.animationName === "hiw-dot-fill") show((current + 1) % captions.length);
    });

    if ("IntersectionObserver" in window) {
      var reveal = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          // Anything already scrolled past counts as seen.
          if (!entry.isIntersecting && entry.boundingClientRect.top > 0) return;
          entry.target.classList.add("is-visible");
          reveal.unobserve(entry.target);
        });
      }, { threshold: 0.15 });
      root.querySelectorAll(".hiw-reveal").forEach(function (item) { reveal.observe(item); });
      // Only plays while on screen, so nobody scrolls past half the story.
      new IntersectionObserver(function (entries) {
        hidden = !entries[0].isIntersecting;
        updatePaused();
      }, { threshold: 0.35 }).observe(stage);
    } else {
      root.querySelectorAll(".hiw-reveal").forEach(function (item) { item.classList.add("is-visible"); });
      hidden = false;
    }
    updatePaused();
    show(0);
  }

  document.addEventListener("DOMContentLoaded", setup);
})();
