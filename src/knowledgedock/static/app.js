(function () {
  "use strict";

  /* ------------------------------------------------------------- theme toggle */
  var toggle = document.getElementById("kdThemeToggle");
  var icon = document.getElementById("kdThemeIcon");
  var root = document.documentElement;

  var applyTheme = function (theme) {
    root.setAttribute("data-bs-theme", theme);
    if (icon) icon.textContent = theme === "dark" ? "☀" : "☾";
    if (toggle) {
      toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      toggle.setAttribute(
        "aria-label",
        theme === "dark" ? "Switch to light theme" : "Switch to dark theme"
      );
    }
  };

  // The inline script in <head> already resolved and applied a theme; read it back
  // rather than resolving a second time, so the button can never disagree with the
  // page it is sitting on.
  applyTheme(root.getAttribute("data-bs-theme") === "dark" ? "dark" : "light");

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem("kd-theme", next);
      } catch (e) {
        /* Private browsing: the toggle still works for this page, it just will
           not be remembered. Not worth surfacing to the user. */
      }
    });
  }

  // Follow the system only while the visitor has not made an explicit choice.
  if (window.matchMedia) {
    var media = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function (event) {
      var stored = null;
      try {
        stored = localStorage.getItem("kd-theme");
      } catch (e) {
        return;
      }
      if (stored) return;
      applyTheme(event.matches ? "dark" : "light");
    };
    if (media.addEventListener) {
      media.addEventListener("change", onSystemChange);
    } else if (media.addListener) {
      media.addListener(onSystemChange);
    }
  }
})();

/* ------------------------------------------------------------ sticky navbar */
(function () {
  "use strict";

  var navbar = document.getElementById("kdNavbar");
  if (!navbar) return;

  var onScroll = function () {
    navbar.classList.toggle("kd-scrolled", window.scrollY > 8);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();

/* ------------------------------------------- confirm before a destructive post */
/* The delete form is a plain POST that redirects, so it carries its confirmation
   as a data attribute and the prompt is raised here by delegation. That keeps the
   markup free of inline handlers and works for rows the status poll swaps in
   later, which a listener bound once at load time would miss. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || typeof form.getAttribute !== "function") return;
    var message = form.getAttribute("data-confirm-delete");
    if (!message) return;
    if (!window.confirm(message)) event.preventDefault();
  });
})();

/* ------------------------------------------------------------------- modal */
/* The upload dialog is marked up with Bootstrap's data-bs-toggle/dismiss
   attributes, but only Bootstrap's CSS is loaded -- there is no bundle script,
   and the brief rules out adding one. Without Bootstrap's JS those attributes do
   nothing at all and the dialog can never open, so the handful of behaviours the
   markup already promises is implemented here directly: backdrop, scroll lock,
   focus move and restore, Escape, and a simple focus trap. */
(function () {
  "use strict";

  var modal = document.getElementById("uploadModal");
  if (!modal) return;

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  var lastFocused = null;
  var backdrop = null;

  /* The trigger is passed in rather than read from document.activeElement:
     not every browser focuses a button when it is clicked (Safari on macOS
     deliberately does not), so activeElement is often still <body> here and the
     dialog would fail to hand focus back on close. */
  var open = function (trigger) {
    if (!modal.hidden) return;
    lastFocused = trigger || document.activeElement;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    /* The `show` class is not decoration. Bootstrap's stylesheet carries
       `.fade:not(.show){opacity:0}`, and the dialog is marked `class="modal fade"`.
       Lifting `hidden` alone fixes `display` (app.css supplies that rule) but
       leaves the element at zero opacity, so the dialog opened as an invisible
       box behind a visible backdrop: one press did nothing a user could see, and
       the second press was swallowed by the early return above. */
    modal.classList.add("show");
    document.body.classList.add("kd-modal-open");

    backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop fade show";
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", close);

    // The dialog itself takes focus: the first tabbable control is the visually
    // hidden file input, and focusing that announces a file field before the user
    // has chosen anything. tabindex="-1" is on the element for this purpose.
    modal.focus();
  };

  var close = function () {
    if (modal.hidden) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    modal.classList.remove("show");
    document.body.classList.remove("kd-modal-open");
    if (backdrop) {
      backdrop.removeEventListener("click", close);
      backdrop.remove();
      backdrop = null;
    }
    if (lastFocused && typeof lastFocused.focus === "function") lastFocused.focus();
    lastFocused = null;
  };

  // The markup is hidden up front, so the dialog is inert before any script runs.
  modal.hidden = true;

  document.addEventListener("click", function (event) {
    if (!event.target || typeof event.target.closest !== "function") return;

    var opener = event.target.closest('[data-bs-toggle="modal"]');
    if (opener) {
      var selector = opener.getAttribute("data-bs-target");
      if (!selector) return;
      var target = document.querySelector(selector);
      if (!target || target !== modal) return;
      event.preventDefault();
      open(opener);
      return;
    }
    var dismisser = event.target.closest('[data-bs-dismiss="modal"]');
    if (dismisser && modal.contains(dismisser)) {
      event.preventDefault();
      close();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (modal.hidden) return;

    if (event.key === "Escape") {
      close();
      return;
    }

    // Keep Tab inside the dialog while it is up.
    if (event.key !== "Tab") return;
    var items = Array.prototype.filter.call(
      modal.querySelectorAll(FOCUSABLE),
      function (el) { return el.offsetParent !== null; }
    );
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
})();

/* ------------------------------------------------------------------ collapse */
/* `data-bs-toggle="collapse"` appeared in the markup for the "New workspace"
   disclosure, and nothing implemented it: only Bootstrap's CSS is loaded, never
   its bundle, so `.collapse:not(.show){display:none}` hid the panel and the
   button was dead. The button looked live, which is the worst way for a control
   to fail.

   Implemented here for the same reason the modal is -- the attributes already
   promise the behaviour, and the alternative is markup that lies. Toggling `show`
   is all Bootstrap's CSS needs; `hidden` is carried alongside it so the collapsed
   panel leaves the accessibility tree even if that stylesheet is unavailable. */
(function () {
  "use strict";

  var panels = function (selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  };

  var setOpen = function (trigger, open) {
    var selector = trigger.getAttribute("data-bs-target");
    if (!selector) return;

    var panel = document.querySelector(selector);
    if (!panel) return;

    panel.classList.toggle("show", open);
    panel.hidden = !open;
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
  };

  document.addEventListener("click", function (event) {
    if (!event.target || typeof event.target.closest !== "function") return;

    var trigger = event.target.closest('[data-bs-toggle="collapse"]');
    if (!trigger) return;
    event.preventDefault();

    var expanded = trigger.getAttribute("aria-expanded") === "true";
    // Only one panel in this app, but a disclosure that left two open would be
    // the kind of thing nobody notices until it ships.
    panels('[data-bs-toggle="collapse"]').forEach(function (other) {
      if (other !== trigger) setOpen(other, false);
    });
    setOpen(trigger, !expanded);
  });

  // Escape closes an open panel, matching the modal's behaviour.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    panels('[data-bs-toggle="collapse"][aria-expanded="true"]').forEach(function (trigger) {
      setOpen(trigger, false);
      trigger.focus();
    });
  });

  // Start from a known state: markup ships collapsed, and a panel left open by a
  // previous render must not inherit `hidden` incorrectly.
  panels('[data-bs-toggle="collapse"]').forEach(function (trigger) {
    setOpen(trigger, trigger.getAttribute("aria-expanded") === "true");
  });
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

/* ------------------------------------------------- upload: in-flight feedback */
/* The dialog's submit button stayed enabled for the whole round trip, so a slow
   upload looked identical to a dead one and the only honest thing to do was to
   press it again. The request is now visibly acknowledged the moment it starts:
   the button disables and says so, and it is restored only if the upload failed,
   because a successful one navigates away. */
(function () {
  "use strict";

  var form = document.getElementById("kdUploadForm");
  if (!form) return;

  var submit = form.querySelector('button[type="submit"]');
  var idle = submit ? submit.textContent : "";
  var label = document.getElementById("kdUploadStatus");
  var quiet = label ? label.textContent : "";
  var filename = document.getElementById("kdFileName");
  var picker = document.getElementById("kdFile");

  form.addEventListener("htmx:beforeRequest", function () {
    if (submit) {
      submit.disabled = true;
      submit.textContent = "Uploading…";
    }
    if (label) label.textContent = "Sending your file. Large PDFs can take a moment.";
  });

  form.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.successful) {
      // The server answers a good upload with HX-Redirect, so the browser is
      // already navigating to the dashboard where the new row appears. Clearing
      // the picker first means a reload does not offer the same file again.
      if (picker) picker.value = "";
      if (filename) filename.textContent = "";
      return;
    }

    // Refused: let the person try again without reopening the dialog.
    if (submit) {
      submit.disabled = false;
      submit.textContent = idle;
    }
    if (label) label.textContent = quiet;
  });
})();

/* ------------------------------------- dashboard polling: swap rows, then stop */
/* The server renders /ui/workspaces/{id}/documents as a bare list of <tr> rows and
   the dashboard swaps them in with innerHTML. A <tr> is not valid directly inside a
   <div>, and in the HTML parser's "in body" mode such a start tag is ignored
   outright -- so an innerHTML swap there silently discards every row and the table
   empties itself a couple of seconds after each upload.

   The fix is to re-home the rows ourselves. Assigning the response to a detached
   <tbody> parses in "in table body" mode, where <tr> is legal, and the rows can
   then be moved into the real tbody. Doing it in beforeSwap rather than
   afterSwap is deliberate: after the default swap has run, the response text is
   gone.

   Polling lives in the same block because it depends on the rows having landed --
   the stop check reads the badges out of the very markup just inserted, so the two
   cannot be separate listeners without racing. */
(function () {
  "use strict";

  var container = document.getElementById("kdDocuments");
  if (!container || !window.htmx) return;

  var note = document.getElementById("kdPollNote");
  var inFlight = ".kd-badge-pending, .kd-badge-processing";

  var reveal = function () {
    if (note) note.hidden = container.querySelectorAll(inFlight).length === 0;
  };

  reveal();

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "kdDocuments") return;
    if (!event.detail.xhr) return;

    // Resolved per event rather than cached: the tbody is absent when the
    // workspace has no documents yet, and the table is what an upload creates.
    var rows = document.getElementById("kdDocumentRows");
    if (!rows) return;

    var holder = document.createElement("tbody");
    holder.innerHTML = event.detail.xhr.responseText;
    rows.replaceChildren.apply(rows, Array.from(holder.childNodes));

    // The rows are placed by hand, so htmx must not also try to swap them.
    event.detail.shouldSwap = false;

    if (container.querySelectorAll(inFlight).length) return;

    // Nothing left in flight: cancel the interval rather than keep polling. Against
    // a free-tier M0 an always-on 4s poll from every open tab is pure waste.
    container.removeAttribute("hx-trigger");
    try {
      htmx.trigger(container, "stopPolling");
    } catch (err) {
      /* Older htmx without stopPolling: dropping the attribute is enough to keep
         the current cycle from rescheduling once it fires. */
    }
    reveal();
  });
})();

/* --------------------------------------------------------- ask: wait experience */
/* An answer can take tens of seconds on the free tier, and a page that does
   nothing is indistinguishable from a broken one. Each ask therefore gets an
   immediate placeholder: the question shown back as a turn plus a "thinking"
   turn with an animated bar and typing dots, so the page visibly works while
   the request is out. The question box and the Ask button are disabled while
   it is in flight so a second Enter cannot stack a request, and the
   placeholder is removed when the real answer (or the error) arrives. */
(function () {
  "use strict";

  var calm = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

  function scrollTo(node) {
    if (node && typeof node.scrollIntoView === "function") {
      node.scrollIntoView({
        block: "nearest",
        behavior: calm && calm.matches ? "auto" : "smooth",
      });
    }
  }

  function turnElement(role, className) {
    var article = document.createElement("article");
    article.className =
      "kd-turn kd-turn-" + role + (className ? " " + className : "");
    var head = document.createElement("div");
    head.className = "kd-turn-role";
    head.textContent = role === "user" ? "You asked" : "Answer";
    article.appendChild(head);
    return article;
  }

  /* The main form and every follow-up form inside an answer share one wait
     experience because they share the same slow request. */
  document.querySelectorAll("#kdAskForm, form.kd-followup").forEach(function (form) {
    var region = document.getElementById("kdAnswer");
    if (!region) return;
    var box = form.querySelector('[name="question"]');
    var button = form.querySelector('button[type="submit"]');
    var asked = null;
    var think = null;

    function busy() {
      if (!box) return;
      var text = box.value.trim();
      if (!text) return;

      var empty = region.querySelector(".kd-empty");
      if (empty) empty.parentNode.removeChild(empty);

      asked = turnElement("user");
      var askedText = document.createElement("p");
      askedText.className = "kd-turn-text";
      askedText.textContent = text;
      asked.appendChild(askedText);

      think = turnElement("assistant", "kd-think");
      think.setAttribute("role", "status");

      var line = document.createElement("p");
      line.className = "kd-think-line";
      line.textContent = "Searching this workspace and reading the documents…";
      think.appendChild(line);

      var bar = document.createElement("div");
      bar.className = "kd-think-bar";
      bar.setAttribute("aria-hidden", "true");
      var fill = document.createElement("span");
      fill.className = "kd-think-bar-fill";
      bar.appendChild(fill);
      think.appendChild(bar);

      var dots = document.createElement("div");
      dots.className = "kd-typing";
      dots.setAttribute("aria-hidden", "true");
      for (var i = 0; i < 3; i += 1) {
        var dot = document.createElement("span");
        dot.className = "kd-typing-dot";
        dots.appendChild(dot);
      }
      think.appendChild(dots);

      // A follow-up form sits inside the previous answer's turn, so the new
      // question belongs after that turn, not after the form's controls.
      var anchor = form.closest("article.kd-turn");
      region.insertBefore(asked, anchor ? anchor.nextSibling : null);
      region.insertBefore(think, asked.nextSibling);

      scrollTo(think);

      box.disabled = true;
      box.setAttribute("aria-busy", "true");
      if (button) button.disabled = true;
    }

    function idle(successful) {
      if (think && think.parentNode) think.parentNode.removeChild(think);
      think = null;
      asked = null;
      if (box) {
        box.disabled = false;
        box.removeAttribute("aria-busy");
        if (successful) box.value = "";
      }
      if (button) button.disabled = false;
      // The asked turn stays either way: the question was stored before the
      // request, so keeping it matches the history and an error fragment that
      // follows it reads as an honest rejection rather than a blank.
      if (region.lastElementChild) scrollTo(region.lastElementChild);
    }

    form.addEventListener("htmx:beforeRequest", busy);
    form.addEventListener("htmx:afterRequest", function (event) {
      idle(event.detail.successful);
    });

    // Enter submits; Shift+Enter inserts a newline. A textarea where Enter
    // silently discards a half-written question is a common way to lose one.
    if (box && box.tagName === "TEXTAREA") {
      box.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          form.requestSubmit();
        }
      });
    }
  });
})();

/* --------------------------------------------------- keep answers in view */
/* A new answer arrives at the bottom of a list that may be scrolled past. Moving
   focus to it would steal the caret mid-typing, so instead the region is brought
   into view and the outcome is announced through its existing aria-live. */
(function () {
  "use strict";

  var region = document.getElementById("kdAnswer");
  if (!region) return;

  var calm = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "kdAnswer") return;
    var last = region.lastElementChild;
    if (last && typeof last.scrollIntoView === "function") {
      last.scrollIntoView({
        block: "nearest",
        behavior: calm && calm.matches ? "auto" : "smooth",
      });
    }
  });
})();

/* ------------------------------------------------------- ask errors swap in */
/* htmx 2's default response handling swaps only 2xx (and 3xx); a 4xx/5xx ask
   response -- a refused question, a provider outage -- is treated as an error
   and dropped, so after the thinking animation the page would go quiet and no
   rejection would ever be seen. The ask target is a conversation, where an
   honest rejection is itself the output, so ask-error responses are forced in. */
(function () {
  "use strict";

  if (!window.htmx || !document.getElementById("kdAnswer")) return;

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    if (!event.detail.isError) return;
    if (!event.detail.target || event.detail.target.id !== "kdAnswer") return;
    event.detail.shouldSwap = true;
  });
})();
