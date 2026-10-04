(function () {
  "use strict";

  var navbar = document.getElementById("kdNavbar");
  if (navbar) {
    var onScroll = function () {
      navbar.classList.toggle("kd-scrolled", window.scrollY > 8);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  var revealEls = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && revealEls.length) {
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("kd-visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    revealEls.forEach(function (el) {
      observer.observe(el);
    });
  } else {
    revealEls.forEach(function (el) {
      el.classList.add("kd-visible");
    });
  }
})();

/* --------------------------------------------------------------- dropzone */
(function () {
  "use strict";

  var zone = document.getElementById("kdDropzone");
  if (!zone) return;
  var input = document.getElementById("kdFile");
  var name = document.getElementById("kdFileName");
  if (!input) return;

  var show = function () {
    if (name && input.files && input.files.length) {
      name.textContent = input.files[0].name;
    }
  };
  input.addEventListener("change", show);

  ["dragenter", "dragover"].forEach(function (event) {
    zone.addEventListener(event, function (e) {
      e.preventDefault();
      zone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (event) {
    zone.addEventListener(event, function (e) {
      e.preventDefault();
      zone.classList.remove("is-dragging");
    });
  });
  zone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      show();
    }
  });
})();

/* --------------------------------------------------- stop polling when idle */
/* The dashboard polls every 4s only while a document is pending or processing.
   Once the last one settles, the interval is cancelled: against a free-tier M0
   an always-on poll from every open tab is pure waste. */
(function () {
  "use strict";

  var container = document.getElementById("kdDocuments");
  if (!container || !window.htmx) return;

  var pending = container.querySelectorAll(".kd-badge-pending, .kd-badge-processing");
  var note = document.getElementById("kdPollNote");
  if (!pending.length) {
    if (note) note.hidden = true;
    return;
  }
  if (note) note.hidden = false;

  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "kdDocumentRows") return;
    var live = container.querySelectorAll(".kd-badge-pending, .kd-badge-processing");
    if (live.length) return;
    container.removeAttribute("hx-trigger");
    try {
      htmx.trigger(container, "stopPolling");
    } catch (err) {
      container.innerHTML = container.innerHTML;
    }
    if (note) note.hidden = true;
  });
})();

/* --------------------------------------------------------- ask: clear + focus */
(function () {
  "use strict";

  var form = document.getElementById("kdAskForm");
  if (!form) return;

  form.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.successful) {
      var box = document.getElementById("kdQuestion");
      if (box) box.value = "";
    }
  });

  // Enter submits; Shift+Enter inserts a newline. A textarea where Enter
  // silently discards a half-written question is a common way to lose one.
  var question = document.getElementById("kdQuestion");
  if (question) {
    question.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }
})();
