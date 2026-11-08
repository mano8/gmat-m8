// Small and CSP-safe: no inline handlers, no eval, nothing written to localStorage.
(function () {
  "use strict";

  // Toasts: the close button removes its message.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss]");
    if (!button) {
      return;
    }
    var target = button.closest(button.getAttribute("data-dismiss"));
    if (target) {
      target.remove();
    }
  });

  // A menu or sheet (a popover) is not modal: when the keyboard moves the focus past its end, it
  // closes instead of staying open over the page the focus has gone to.
  document.addEventListener("focusin", function (event) {
    document.querySelectorAll("[popover]:popover-open").forEach(function (popover) {
      if (popover.getAttribute("popover") === "manual" || popover.contains(event.target)) {
        return;
      }
      var invoker = popover.id ? document.querySelector('[popovertarget="' + popover.id + '"]') : null;
      if (!invoker || !invoker.contains(event.target)) {
        popover.hidePopover();
      }
    });
  });

  // Success messages fade after a few seconds.
  window.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-autohide]").forEach(function (node) {
      var delay = parseInt(node.getAttribute("data-autohide"), 10) || 6000;
      window.setTimeout(function () {
        node.remove();
      }, delay);
    });
  });

  // A print button (the purchase-order page): without JavaScript the browser's own print
  // command does the same.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) {
      window.print();
    }
  });

  // A form marked data-once disables its buttons once sent, so a double tap posts once. The
  // idempotency key in the form is the real guard; this only spares the second request. A page
  // restored from the back/forward cache enables them again.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-once") || form.hasAttribute("hx-post")) {
      return;
    }
    window.setTimeout(function () {
      form.querySelectorAll("button[type=submit]").forEach(function (button) {
        button.disabled = true;
      });
    }, 0);
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      document.querySelectorAll("form[data-once] button[type=submit]").forEach(function (button) {
        button.disabled = false;
      });
    }
  });

  // A list refreshed through htmx: the toolbar's live region announces the new result count
  // (the count itself sits in the swapped results, which a screen reader would not announce).
  // A page with several lists wraps each one (toolbar and results) in a [data-list-section].
  document.addEventListener("htmx:afterSettle", function (event) {
    // Only a swap that brings a result count (not a delivery badge polling every few seconds).
    var swapped = event.target;
    if (!(swapped.matches && (swapped.matches("[data-list-count]") || swapped.querySelector("[data-list-count]")))) {
      return;
    }
    var scope = (event.target.closest && event.target.closest("[data-list-section]")) || document;
    var status = scope.querySelector("[data-list-status]");
    var count = scope.querySelector("[data-list-count]");
    if (status && count) {
      status.textContent = count.textContent.trim();
    }
  });

  // An htmx answer that was redirected (the session ended or needs MFA again: the sign-in page)
  // never lands inside the page. The asked and answered paths are compared, as the browser
  // follows a redirect before htmx sees the answer.
  function redirected(detail) {
    var asked = new URL(detail.requestConfig.path, window.location.href).pathname;
    var answered = detail.xhr.responseURL ? new URL(detail.xhr.responseURL).pathname : asked;
    return answered !== asked;
  }

  // A badge that reloads itself (data-poll, the email delivery status): only a 200 from the
  // URL it asked may replace it. A redirect or an error keeps the badge as it is and stops the
  // polling, which htmx does once the element has left the page.
  function stopPolling(event) {
    var element = event.detail.elt;
    if (event.detail.xhr.status === 200 && !redirected(event.detail)) {
      return;
    }
    event.detail.shouldSwap = false;
    var still = element.cloneNode(true);
    ["hx-get", "hx-trigger", "hx-swap", "data-poll"].forEach(function (name) {
      still.removeAttribute(name);
    });
    element.replaceWith(still);
  }

  // Any other htmx request (a list, a dialog's form) that was redirected: the whole page goes
  // where the answer went (the sign-in page), instead of drawing it inside the page.
  document.addEventListener("htmx:beforeSwap", function (event) {
    var element = event.detail.elt;
    if (element.hasAttribute && element.hasAttribute("data-poll")) {
      stopPolling(event);
      return;
    }
    if (redirected(event.detail)) {
      event.detail.shouldSwap = false;
      window.location.assign(event.detail.xhr.responseURL);
    }
  });

  // --- Dialogs (M8b.3) ------------------------------------------------------------------------
  // Native <dialog> elements opened by invoker commands (commandfor/command). Where the browser
  // lacks them, this opens and closes the dialog the same way. Focus goes back to the opener.
  var nativeCommands = "commandForElement" in HTMLButtonElement.prototype;
  var openers = new WeakMap();

  function dialogOf(element) {
    return element && element.closest ? element.closest("dialog[data-dialog]") : null;
  }

  function failure(dialog, shown) {
    var message = dialog.querySelector("[data-dialog-failure]");
    var content = dialog.querySelector("[data-dialog-content]");
    if (message) {
      message.hidden = !shown;
    }
    if (content) {
      content.hidden = shown;
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[commandfor]");
    if (!button) {
      return;
    }
    var dialog = document.getElementById(button.getAttribute("commandfor"));
    if (!dialog || dialog.tagName !== "DIALOG") {
      return;
    }
    var command = button.getAttribute("command");
    if (command === "show-modal") {
      // An item of a menu (a popover): the menu closes and the focus later returns to the
      // button that opened the menu, since the item itself is hidden by then.
      var menu = button.closest("[popover]");
      var returnTo = button;
      if (menu) {
        var invoker = menu.id ? document.querySelector('[popovertarget="' + menu.id + '"]') : null;
        if (menu.matches(":popover-open")) {
          menu.hidePopover();
        }
        returnTo = invoker || button;
      }
      openers.set(dialog, returnTo);
      if (!nativeCommands && !dialog.open) {
        dialog.showModal();
      }
    } else if (command === "close" && !nativeCommands && dialog.open) {
      dialog.close();
    }
  });

  // A lazy dialog: its opener is a link to the form's own page that htmx loads into the dialog
  // (hx-get, hx-target). The dialog opens at once with a placeholder; without htmx the link
  // simply opens the page.
  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog-open]");
    if (!opener || !window.htmx) {
      return;
    }
    var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
    if (!dialog || dialog.tagName !== "DIALOG") {
      return;
    }
    var content = dialog.querySelector("[data-dialog-content]");
    var placeholder = dialog.querySelector("template[data-dialog-placeholder]");
    if (content && placeholder) {
      content.replaceChildren(placeholder.content.cloneNode(true));
    }
    failure(dialog, false);
    openers.set(dialog, opener);
    if (!dialog.open) {
      dialog.showModal();
    }
  });

  // Closing (Esc, the close button, Cancel): focus returns to what opened the dialog.
  document.addEventListener(
    "close",
    function (event) {
      var dialog = event.target;
      if (!dialog.matches || !dialog.matches("dialog[data-dialog]")) {
        return;
      }
      var opener = openers.get(dialog);
      if (opener && opener.isConnected) {
        opener.focus();
      }
    },
    true
  );

  // A form swapped back into a dialog with its errors (422) or the reason it is out of date
  // (409): the summary takes the focus (role="alert" announces it); its links lead to the fields.
  document.addEventListener("htmx:afterSettle", function (event) {
    var dialog = dialogOf(event.detail.target);
    if (!dialog) {
      return;
    }
    failure(dialog, false);
    var summary = event.detail.target.querySelector("[data-error-summary]");
    if (summary) {
      summary.focus();
    }
  });
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-error-summary] a[data-error-field]");
    var field = link && document.getElementById(link.getAttribute("data-error-field"));
    if (field) {
      event.preventDefault();
      field.focus();
    }
  });

  // An answer htmx does not swap (400, 403, 404, 5xx) or no answer at all: the dialog says so,
  // with the way out (reload the page), instead of an error page drawn inside it.
  function dialogFailed(event) {
    var dialog = dialogOf(event.detail.target) || dialogOf(event.detail.elt);
    if (dialog) {
      failure(dialog, true);
    }
  }
  document.addEventListener("htmx:responseError", dialogFailed);
  document.addEventListener("htmx:sendError", dialogFailed);

  // Display preferences (M8b.2, Q69). The server renders the remembered state and writes the
  // cookie; the script only paints a choice at once. Nothing here reads or writes a cookie.
  //
  // The left menu's button: hide or show the menu now. The button's value is the state it asks
  // for, so it flips only once the request is done.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-menu-toggle]");
    if (!button) {
      return;
    }
    var hide = button.value === "hidden";
    var menu = document.getElementById(button.getAttribute("aria-controls"));
    if (menu) {
      menu.hidden = hide;
    }
    button.setAttribute("aria-expanded", hide ? "false" : "true");
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var button = event.detail.elt.querySelector && event.detail.elt.querySelector("[data-menu-toggle]");
    if (!button) {
      return;
    }
    var hidden = button.getAttribute("aria-expanded") === "false";
    button.value = hidden ? "shown" : "hidden";
    button.setAttribute("aria-label", button.getAttribute(hidden ? "data-label-show" : "data-label-hide"));
  });

  // The theme switch: System, Light or Dark takes effect without a reload. The server writes the
  // same on <html> for the next page, so the first paint is right.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-switch] button[name=theme]");
    if (!button) {
      return;
    }
    var theme = button.value;
    var root = document.documentElement;
    if (theme === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
    var scheme = document.querySelector("meta[name=color-scheme]");
    if (scheme) {
      scheme.setAttribute("content", theme === "system" ? "light dark" : theme);
    }
    document.querySelectorAll("[data-theme-switch] button[name=theme]").forEach(function (other) {
      other.setAttribute("aria-pressed", other.value === theme ? "true" : "false");
    });
    document.dispatchEvent(new CustomEvent("gmat:theme", { detail: { theme: theme } }));
  });

  // A category of the menu folded or unfolded by the person (not the page opening the current one):
  // remembered through the same endpoint. Without JavaScript the dropdowns still fold for the page.
  document.addEventListener("click", function (event) {
    var summary = event.target.closest("[data-nav-group] > summary");
    if (summary) {
      summary.parentElement.setAttribute("data-user-toggle", "1");
    }
  });
  document.addEventListener(
    "toggle",
    function (event) {
      var group = event.target;
      if (!group.matches || !group.matches("[data-nav-group]") || !group.hasAttribute("data-user-toggle")) {
        return;
      }
      group.removeAttribute("data-user-toggle");
      var state = group.open ? "open" : "closed";
      if (state === group.getAttribute("data-saved") || !window.htmx) {
        return;
      }
      group.setAttribute("data-saved", state);
      window.htmx.ajax("POST", group.getAttribute("data-prefs-url"), {
        source: group,
        swap: "none",
        values: { fold: group.getAttribute("data-nav-group"), state: state },
      });
    },
    true
  );

  // The language switcher submits when the selection changes (without JavaScript the form
  // still has its button).
  document.addEventListener("change", function (event) {
    var select = event.target.closest("select[data-autosubmit]");
    if (select && select.form) {
      select.form.requestSubmit();
    }
  });
})();
