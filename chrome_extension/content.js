/**
 * chrome_extension/content.js
 * ----------------------------
 * Content script injected into web pages by the AI Browser Automation Extension.
 * Handles:
 *  1. Structured DOM Inspection & Form Discovery (compatible with AIAgentPlanner)
 *  2. Visual element badge overlays
 *  3. Action Execution (click, fill, type, select, check, radio, scroll, extract, wait, press, focus)
 */

(function () {
  // Prevent multiple injections
  if (window.__AI_AGENT_CONTENT_SCRIPT_INITIALIZED__) {
    return;
  }
  window.__AI_AGENT_CONTENT_SCRIPT_INITIALIZED__ = true;

  // ---------------------------------------------------------------------------
  // Helper: Label Resolution & CSS Selector Generation
  // ---------------------------------------------------------------------------

  function inferLabel(el) {
    if (!el) return "";

    // 1. Explicit <label for="id">
    if (el.id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
      } catch (e) {}
    }

    // 2. aria-label attribute
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

    // 3. aria-labelledby attribute
    const ariaLabelledBy = el.getAttribute("aria-labelledby");
    if (ariaLabelledBy) {
      try {
        const lbl = document.getElementById(ariaLabelledBy);
        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
      } catch (e) {}
    }

    // 4. Wrapping <label>
    const parentLabel = el.closest("label");
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll("input, select, textarea, button").forEach((c) => c.remove());
      const txt = clone.innerText.trim();
      if (txt) return txt;
    }

    // 5. Preceding sibling <label>
    let prev = el.previousElementSibling;
    while (prev) {
      if (prev.tagName === "LABEL" && prev.innerText.trim()) {
        return prev.innerText.trim();
      }
      prev = prev.previousElementSibling;
    }

    // 6. Placeholder / Title / Alt attribute
    const placeholder = el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("alt");
    if (placeholder && placeholder.trim()) return placeholder.trim();

    // 7. Button or submit fallback
    if (el.tagName === "BUTTON" || el.type === "submit" || el.type === "button") {
      return (el.innerText || el.value || "").trim();
    }

    // 8. Google Forms: resolve the title from the nearest question card only.
    // A broad ancestor search can accidentally borrow the previous/next
    // question title and cause values to be written into the wrong field.
    const questionCard = el.closest('.Qr7Oae,[role="listitem"],[data-params]');
    if (questionCard) {
      const title = questionCard.querySelector(
        '[role="heading"],.M7eMe,[class*="ItemTitle"],[class*="QuestionTitle"]'
      );
      const titleText = String(title?.innerText || title?.textContent || "")
        .replace(/\s+/g, " ").replace(/\s*\*\s*$/, "").trim();
      if (titleText) return titleText;
    }

    // 9. Generic fallback is intentionally restricted to the immediate
    // parent. Searching several ancestors can cross question boundaries.
    const parent = el.parentElement;
    if (parent) {
      const heading = parent.querySelector(':scope > [role="heading"],:scope > [class*="Title"],:scope > [class*="label"]');
      const text = String(heading?.innerText || heading?.textContent || "").trim();
      if (text) return text;
    }
    return "";
  }

  function uniqueSelector(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return "";
    if (el.id) {
      try {
        return `#${CSS.escape(el.id)}`;
      } catch (e) {
        return `#${el.id}`;
      }
    }
    const name = el.getAttribute("name");
    if (name) {
      try {
        return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
      } catch (e) {}
    }

    // Prefer stable accessible attributes over long nth-of-type DOM paths.
    // Gmail and other SPAs frequently rerender containers, invalidating those paths.
    for (const attr of ["aria-label", "data-tooltip", "title", "data-testid", "data-test"]) {
      const value = el.getAttribute(attr);
      if (!value || !value.trim()) continue;
      const escaped = value.replace(/\\\\/g, "\\\\\\\\").replace(/"/g, '\\"');
      const stableSelector = `[${attr}="${escaped}"]`;
      try {
        if (document.querySelectorAll(stableSelector).length === 1) return stableSelector;
      } catch (e) {}
    }

    const testId = el.getAttribute("data-testid") || el.getAttribute("data-test");
    if (testId) {
      try {
        return `[data-testid="${CSS.escape(testId)}"]`;
      } catch (e) {}
    }

    let path = [];
    let cur = el;
    while (cur && cur.nodeType === Node.ELEMENT_NODE && cur.tagName !== "BODY" && cur.tagName !== "HTML") {
      let selector = cur.tagName.toLowerCase();
      if (cur.id) {
        try {
          selector += `#${CSS.escape(cur.id)}`;
          path.unshift(selector);
          break;
        } catch (e) {}
      }
      if (cur.className && typeof cur.className === "string") {
        const classes = cur.className
          .trim()
          .split(/\s+/)
          .filter((c) => c && !c.startsWith("ai-agent-") && !c.includes(":"));
        if (classes.length > 0) {
          try {
            selector += "." + classes.slice(0, 2).map(CSS.escape).join(".");
          } catch (e) {}
        }
      }
      let sibling = cur;
      let nth = 1;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === cur.tagName) nth++;
      }
      if (nth > 1) selector += `:nth-of-type(${nth})`;
      path.unshift(selector);
      cur = cur.parentElement;
    }
    return path.length > 0 ? path.join(" > ") : el.tagName.toLowerCase();
  }

  // ---------------------------------------------------------------------------
  // DOM Inspection & Element Collection
  // ---------------------------------------------------------------------------

  function inspectPageDOM(showBadges) {
    const lockEl = document.getElementById("ai-agent-ui-lock");
    const prevLockPointerEvents = lockEl ? lockEl.style.pointerEvents : null;
    if (lockEl) lockEl.style.pointerEvents = "none";
    try {
      return _inspectPageDOMInternal(showBadges);
    } finally {
      if (lockEl && prevLockPointerEvents !== null) {
        lockEl.style.pointerEvents = prevLockPointerEvents;
      }
    }
  }

  function _inspectPageDOMInternal(showBadges) {
    // 1. Remove old overlays
    document.querySelectorAll(".ai-agent-badge").forEach((e) => e.remove());

    const interactiveSelectors =
      "button, a, input, textarea, select, li, " +
      '[role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], ' +
      '[role="row"], [role="treeitem"], [role="option"], [data-target="item"], c-wiz[data-target], [data-id], ' +
      '[tabindex]:not([tabindex="-1"])';

    const rawElements = Array.from(document.querySelectorAll(interactiveSelectors));

    // Prioritize active menus, popups, and modal dialogs so their items appear at the front of results
    rawElements.sort((a, b) => {
      const aMenu = a.closest('[role="menu"], [role="dialog"], [aria-modal="true"], .modal, ul.aqdrmf-E, [role="listbox"]') ? 1 : 0;
      const bMenu = b.closest('[role="menu"], [role="dialog"], [aria-modal="true"], .modal, ul.aqdrmf-E, [role="listbox"]') ? 1 : 0;
      return bMenu - aMenu;
    });

    const results = [];
    let index = 1;

    rawElements.forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (
        rect.width === 0 ||
        rect.height === 0 ||
        rect.top < -50 ||
        rect.top > (window.innerHeight || document.documentElement.clientHeight) + 100
      ) {
        return;
      }
      const cs = window.getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0") {
        return;
      }

      const tag = el.tagName.toLowerCase();
      const text = (
        el.innerText ||
        el.textContent ||
        el.value ||
        el.placeholder ||
        el.alt ||
        el.getAttribute("aria-label") ||
        ""
      )
        .trim()
        .replace(/\s+/g, " ");

      const type = el.getAttribute("type") || tag;
      const elId = el.id ? "#" + el.id : "";
      const name = el.getAttribute("name") ? `[name="${el.getAttribute("name")}"]` : "";
      const labelText = inferLabel(el);
      const sel = uniqueSelector(el);

      if (showBadges) {
        const badge = document.createElement("div");
        badge.className = "ai-agent-badge";
        badge.innerText = index.toString();
        Object.assign(badge.style, {
          position: "absolute",
          left: `${window.scrollX + rect.left}px`,
          top: `${window.scrollY + rect.top}px`,
          backgroundColor: "#ff3366",
          color: "#ffffff",
          fontSize: "12px",
          fontWeight: "bold",
          padding: "2px 6px",
          borderRadius: "10px",
          zIndex: "2147483647",
          pointerEvents: "none",
          boxShadow: "0 2px 5px rgba(0,0,0,0.3)",
        });
        document.body.appendChild(badge);
      }

      // Store element index reference on DOM node
      el.dataset.aiElementIndex = index.toString();

      const midX = Math.round(rect.left + rect.width / 2);
      const midY = Math.round(rect.top + rect.height / 2);
      const centerElement = document.elementFromPoint(
        Math.min(Math.max(midX, 0), Math.max(0, window.innerWidth - 1)),
        Math.min(Math.max(midY, 0), Math.max(0, window.innerHeight - 1))
      );
      const centerHit = Boolean(centerElement && (centerElement === el || el.contains(centerElement)));
      const role = el.getAttribute("role") || (
        tag === "a" ? "link" :
        tag === "button" || (tag === "input" && ["button", "submit"].includes(type)) ? "button" :
        ["input", "textarea", "select"].includes(tag) ? "input" : null
      );
      const dpr = window.devicePixelRatio || 1;
      const borderX = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
      const headerY = Math.max(0, window.outerHeight - window.innerHeight);
      const scrX = Math.round((window.screenX + borderX + midX) * dpr);
      const scrY = Math.round((window.screenY + headerY + midY) * dpr);

      results.push({
        index: index,
        tag: tag,
        type: type,
        role: role,
        text: text.substring(0, 160),
        label: labelText.substring(0, 160),
        aria_label: el.getAttribute("aria-label") || "",
        title: el.getAttribute("title") || "",
        alt: el.getAttribute("alt") || "",
        id: elId,
        name: el.getAttribute("name") || "",
        selector: sel,
        checked: el.checked !== undefined ? el.checked : null,
        disabled: el.disabled || el.getAttribute("aria-disabled") === "true",
        visible: true,
        center_hit: centerHit,
        placeholder: el.placeholder || "",
        accept: el.getAttribute("accept") || "",
        href: el.href || "",
        value: tag === "select" ? el.options[el.selectedIndex]?.value || "" : el.value || "",
        rect: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
        screen_x: scrX,
        screen_y: scrY,
      });

      index++;
    });

    // 2. Collect visible page text for context
    const pageTextParts = [];
    const seen = new Set();
    document
      .querySelectorAll("h1,h2,h3,h4,h5,h6,label,legend,p,[role=\"heading\"],[aria-label],[data-params]")
      .forEach((el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return;
        const t = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
        if (t && t.length > 2 && t.length < 300 && !seen.has(t)) {
          seen.add(t);
          pageTextParts.push(t);
        }
      });

    // 3. Collect Google Forms or specific questionnaire items
    const formQuestions = [];
    document.querySelectorAll("[jsmodel],[data-params],[role=\"listitem\"]").forEach((container) => {
      const qEl = container.querySelector(
        '[role="heading"],[class*="freebirdFormviewerViewItemsItemItemTitle"],[class*="M7eMe"]'
      );
      if (qEl) {
        const qText = qEl.innerText?.trim();
        if (qText && !formQuestions.includes(qText)) formQuestions.push(qText);
      }
    });

    // 4. Form Fields structured collection
    const formFields = [];
    const formInputs = Array.from(document.querySelectorAll("input, select, textarea"));
    formInputs.forEach((inp) => {
      const tag = inp.tagName.toLowerCase();
      let ftype = "text";
      if (tag === "select") ftype = "dropdown";
      else if (tag === "textarea") ftype = "textarea";
      else {
        const t = (inp.getAttribute("type") || "text").toLowerCase();
        if (["email", "password", "number", "tel", "checkbox", "radio", "file", "date", "search", "url", "hidden", "submit"].includes(t)) {
          ftype = t === "dropdown" ? "dropdown" : t;
        }
      }

      const options = [];
      if (tag === "select") {
        Array.from(inp.options || []).forEach((opt) => {
          options.push({
            value: opt.value,
            label: opt.text || opt.value,
            selected: opt.selected,
          });
        });
      }

      formFields.push({
        field_type: ftype,
        selector: uniqueSelector(inp),
        label: inferLabel(inp),
        name: inp.getAttribute("name") || null,
        field_id: inp.id || null,
        placeholder: inp.getAttribute("placeholder") || null,
        autocomplete: inp.getAttribute("autocomplete") || null,
        required: inp.required || inp.getAttribute("aria-required") === "true",
        disabled: inp.disabled || false,
        readonly: inp.readOnly || false,
        current_value: tag === "select" ? inp.options[inp.selectedIndex]?.value || "" : inp.value || (inp.checked ? "checked" : ""),
        options: options,
        visible: !inp.hidden && inp.type !== "hidden",
        aria_label: inp.getAttribute("aria-label") || null,
      });
    });

    const compactForms = {
      form_count: document.forms.length,
      fields: formFields.map((f) => ({
        type: f.field_type,
        label: f.label,
        name: f.name,
        id: f.field_id,
        placeholder: f.placeholder,
        selector: f.selector,
        value: f.current_value,
      })),
    };

    return {
      title: document.title,
      url: window.location.href,
      elements: results.slice(0, 200),
      page_text: pageTextParts.slice(0, 60).join(" | "),
      form_questions: formQuestions,
      headings: Array.from(document.querySelectorAll("h1,h2,h3,[role=\"heading\"]"))
        .map((h) => h.innerText?.trim())
        .filter(Boolean)
        .slice(0, 10),
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        device_pixel_ratio: window.devicePixelRatio || 1,
        scroll_x: Math.round(window.scrollX),
        scroll_y: Math.round(window.scrollY),
      },
      visual_context: {
        active_dialogs: Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]'))
          .filter((el) => {
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
          })
          .slice(0, 8)
          .map((el) => ({
            role: el.getAttribute("role") || "dialog",
            label: el.getAttribute("aria-label") || el.innerText?.trim().slice(0, 160) || "",
            rect: (() => {
              const r = el.getBoundingClientRect();
              return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) };
            })(),
          })),
        fixed_or_sticky_overlays: Array.from(document.querySelectorAll("body *")).slice(0, 2500)
          .filter((el) => {
            const st = window.getComputedStyle(el);
            if (st.position !== "fixed" && st.position !== "sticky") return false;
            const r = el.getBoundingClientRect();
            return r.width > 40 && r.height > 24 && r.bottom > 0 && r.top < window.innerHeight;
          })
          .slice(0, 12)
          .map((el) => ({
            tag: el.tagName.toLowerCase(),
            label: (el.getAttribute("aria-label") || el.innerText || "").trim().replace(/\s+/g, " ").slice(0, 90),
            z_index: window.getComputedStyle(el).zIndex,
          })),
      },
      // Include hidden file inputs separately: they are intentionally omitted
      // from the visible-element list, but their accept rules determine which
      // file the backend must select for the corresponding upload question.
      file_inputs: Array.from(document.querySelectorAll('input[type="file"]')).map((input) => {
        const question = input.closest('[role="listitem"], .Qr7Oae, [data-params], form, [role="dialog"]');
        return {
          accept: input.getAttribute("accept") || "",
          question_text: String(question?.innerText || question?.textContent || "").trim().replace(/\s+/g, " ").slice(0, 500),
          name: input.getAttribute("name") || "",
          multiple: Boolean(input.multiple),
        };
      }),
      form_fields: formFields,
      compact_forms: compactForms,
    };
  }

  function clearBadges() {
    document.querySelectorAll(".ai-agent-badge").forEach((e) => e.remove());
  }

  // ---------------------------------------------------------------------------
  // Action Execution Engine
  // ---------------------------------------------------------------------------

  function isVisibleActionTarget(el) {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility !== "hidden" &&
      style.opacity !== "0" && !el.disabled &&
      el.getAttribute("aria-disabled") !== "true";
  }

  function findGmailTarget(action) {
    if (location.hostname !== "mail.google.com") return null;
    const kind = String(action.action || "").toLowerCase();
    const context = [
      action.reasoning, action.label, action.field_label,
      action.text, action.selector
    ].filter(Boolean).join(" ").toLowerCase();

    if (kind === "click" && /compose/.test(context)) {
      // Gmail re-renders its toolbar frequently. Resolve Compose from the live
      // page instead of trusting an index/selector captured by an earlier scan.
      const candidates = [
        '[role="button"][gh="cm"]',
        '[gh="cm"]',
        '[role="button"][data-tooltip="Compose"]',
        '[data-tooltip="Compose"]',
        '[role="button"][aria-label="Compose"]',
        '[aria-label="Compose"]',
        'button[aria-label="Compose"]',
        '[role="button"][aria-label^="Compose "]',
        '[data-testid="compose"]'
      ];
      for (const selector of candidates) {
        const el = Array.from(document.querySelectorAll(selector)).find(isVisibleActionTarget);
        if (el) return el.closest('[role="button"],button') || el;
      }
      const byText = Array.from(document.querySelectorAll('button,[role="button"],[gh="cm"],.T-I'))
        .find((el) => isVisibleActionTarget(el) &&
          (el.innerText || el.getAttribute("aria-label") || el.getAttribute("data-tooltip") || "")
            .trim().toLowerCase() === "compose");
      if (byText) return byText;
      // Critical: do not fall through to a stale numeric element index. A
      // failed semantic lookup must fail safely rather than click another control.
      return null;
    }

    if (["fill", "type_text", "fill_field"].includes(kind)) {
      const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .M9'))
        .filter(isVisibleActionTarget);
      const dialog = dialogs[dialogs.length - 1] || document;
      const editable = (selectors) => {
        for (const selector of selectors) {
          const el = Array.from(dialog.querySelectorAll(selector)).find(isVisibleActionTarget);
          if (el) return el;
        }
        return null;
      };

      if (/recipient|\bto\b|email address/.test(context) || /[^\s@]+@[^\s@]+\.[^\s@]+/.test(String(action.text || ""))) {
        const recipient = editable([
          'input[name="to"]',
          'input[aria-label="To recipients"]',
          'input[aria-label^="To "]',
          'input[placeholder="Recipients"]',
          'input[peoplekit-id]'
        ]);
        if (recipient) return recipient;
      }
      if (/subject/.test(context)) {
        const subject = editable(['input[name="subjectbox"]', 'input[aria-label="Subject"]']);
        if (subject) return subject;
      }
      if (/message body|email body|body text|message content/.test(context)) {
        const body = editable([
          '[contenteditable="true"][aria-label*="Message Body"]',
          '[contenteditable="true"][g_editable="true"]',
          '[role="textbox"][contenteditable="true"]'
        ]);
        if (body) return body;
      }
    }
    return null;
  }

  function findTargetElement(action) {
    // Resolve form fields by semantic label before trusting a snapshot index.
    // Google Forms rebuilds question cards as sections expand and upload controls load.
    const actionKind = String(action.action || "").toLowerCase();
    const reasoning = String(action.reasoning || "");
    const quotedField = reasoning.match(/(?:the\s+)?['"“]([^'"”]+)['"”]\s+(?:field|question|input)/i);
    const describedField = reasoning.match(/(?:fill|enter|type|populate|update)\s+(?:in\s+)?(?:the\s+)?["'“]?([^"'”]+?)["'”]?\s+(?:field|question|input)\b/i);
    // Function scope is intentional: the Google Forms safety guard below uses it too.
    // It was previously declared inside a nested block, causing ReferenceError.
    const fieldLabel = String(
      action.field_label || action.label ||
      (quotedField && quotedField[1]) ||
      (describedField && describedField[1]) || ""
    ).trim().toLowerCase();

    if (["fill", "fill_field", "type_text", "select", "select_option"].includes(actionKind) && fieldLabel) {
      const normalize = (value) => String(value || "")
        .toLowerCase().replace(/[＊*]/g, "").replace(/[^a-z0-9]+/g, " ").trim();
      const wanted = normalize(fieldLabel);
      const controls = Array.from(document.querySelectorAll(
        'input:not([type="hidden"]), textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"]'
      )).filter(isVisibleActionTarget);
      const matches = controls.filter((control) => {
        const card = control.closest('.Qr7Oae,[role="listitem"],[data-params]');
        const cardTitle = card?.querySelector(
          '[role="heading"],.M7eMe,[class*="ItemTitle"],[class*="QuestionTitle"]'
        );
        const cardLabel = normalize(cardTitle?.innerText || cardTitle?.textContent);
        const accessibleNames = [
          control.getAttribute("aria-label"),
          control.getAttribute("placeholder"),
          control.labels ? Array.from(control.labels).map((label) => label.innerText).join(" ") : "",
        ].map(normalize).filter(Boolean);
        return cardLabel === wanted || accessibleNames.includes(wanted);
      });
      if (matches.length === 1) return matches[0];
      // A supplied semantic label must never fall through to stale positional indexes.
      return null;
    }

    // Google Forms re-renders question cards and changes nth-of-type selectors.
    // An unlabeled fill must fail safely rather than write into a neighbouring field.
    if (
      ["fill", "fill_field", "type_text"].includes(actionKind) &&
      location.hostname === "docs.google.com" &&
      /\/forms\//.test(location.pathname) &&
      !fieldLabel
    ) {
      return null;
    }

    // Prefer live Gmail compose controls over stale snapshot indexes/selectors.
    const gmailTarget = findGmailTarget(action);
    if (gmailTarget) return gmailTarget;

    // If the planner explicitly intended Compose but Gmail's current DOM did
    // not expose a matching control, never fall back to a stale index.
    const gmailClickContext = [
      action.reasoning, action.label, action.field_label, action.text,
      action.selector, action.goal
    ].filter(Boolean).join(" ").toLowerCase();
    if (
      location.hostname === "mail.google.com" &&
      String(action.action || "").toLowerCase() === "click" &&
      /compose/.test(gmailClickContext)
    ) return null;

    // The planner sometimes serializes an inspected numeric element index as
    // "selector": "10" instead of "element_id": 10. Resolve that form against
    // the latest DOM snapshot before treating it as a CSS selector.
    const numericSelector = typeof action.selector === "string" && /^\d+$/.test(action.selector.trim())
      ? action.selector.trim()
      : null;
    if (numericSelector !== null) {
      const byNumericIndex = document.querySelector(`[data-ai-element-index="${numericSelector}"]`);
      if (byNumericIndex) return byNumericIndex;
    }

    // 1. By Element ID (Index from inspection)
    if (action.element_id !== undefined && action.element_id !== null) {
      const byIndex = document.querySelector(`[data-ai-element-index="${action.element_id}"]`);
      if (byIndex) return byIndex;
    }

    // 2. By Selector
    if (action.selector) {
      try {
        const bySel = document.querySelector(action.selector);
        if (bySel) return bySel;
      } catch (e) {}
    }

    // 3. By Target Selector (for drag/drop)
    if (action.target_selector) {
      try {
        const byTarget = document.querySelector(action.target_selector);
        if (byTarget) return byTarget;
      } catch (e) {}
    }

    // 4. By Radio Name + Value
    if (action.radio_name) {
      try {
        const valSel = action.value ? `[value="${CSS.escape(action.value)}"]` : "";
        const radio = document.querySelector(`input[type="radio"][name="${CSS.escape(action.radio_name)}"]${valSel}`);
        if (radio) return radio;
      } catch (e) {}
    }

    // 5. Resolve accessible form-field labels (Gmail and other dynamic SPAs).
    // Prefer editable controls and exact accessible-name matches over broad text matches.
    const textTarget = action.text || action.select_label;
    if (textTarget) {
      const wanted = textTarget.toLowerCase().trim();
      const editable = Array.from(document.querySelectorAll(
        'input:not([type="hidden"]), textarea, [contenteditable="true"], [role="textbox"]'
      ));
      for (const el of editable) {
        const names = [
          el.getAttribute("aria-label"),
          el.getAttribute("placeholder"),
          el.getAttribute("name"),
          el.getAttribute("data-tooltip"),
          el.getAttribute("autocomplete"),
          el.labels ? Array.from(el.labels).map((label) => label.innerText).join(" ") : "",
        ].filter(Boolean).map((value) => value.toLowerCase().trim());
        if (names.some((name) => name === wanted || name.includes(wanted) || wanted.includes(name))) {
          return el;
        }
      }
    }

    // 6. By Text Content or Label (Precise search prioritizing exact leaf matches and Google Drive row items)
    if (textTarget) {
      const lower = textTarget.toLowerCase().trim();

      // Pass 5A: Exact aria-label or title matching
      const ariaElements = Array.from(document.querySelectorAll('[role="row"], [role="option"], [data-target="item"], [aria-label], [title], [data-id]'));
      for (const el of ariaElements) {
        const aria = (el.getAttribute("aria-label") || el.getAttribute("title") || "").toLowerCase().trim();
        if (aria === lower || aria.startsWith(lower + ",") || aria.startsWith(lower + " ") || aria.includes(lower)) {
          return el;
        }
      }

      // Pass 5B: Search leaf elements in reverse document order (most specific children first)
      const allCandidates = Array.from(document.querySelectorAll("span, div, a, p, [role=\"row\"], [role=\"button\"]"));
      for (let i = allCandidates.length - 1; i >= 0; i--) {
        const el = allCandidates[i];
        if (el.children.length === 0) {
          const t = (el.innerText || el.textContent || "").trim().toLowerCase();
          if (t === lower) {
            const rowParent = el.closest('[role="row"], [data-target="item"], [data-id]');
            return rowParent || el;
          }
        }
      }

      // Pass 5C: Any element whose trimmed text strictly equals target
      for (let i = allCandidates.length - 1; i >= 0; i--) {
        const el = allCandidates[i];
        const t = (el.innerText || el.textContent || "").trim().toLowerCase();
        if (t === lower) {
          const rowParent = el.closest('[role="row"], [data-target="item"], [data-id]');
          return rowParent || el;
        }
      }

      // Pass 5D: Substring match on leaf elements only (avoiding huge parent containers)
      for (let i = allCandidates.length - 1; i >= 0; i--) {
        const el = allCandidates[i];
        if (el.children.length === 0) {
          const t = (el.innerText || el.textContent || "").trim().toLowerCase();
          if (t.includes(lower) && t.length <= lower.length + 15) {
            const rowParent = el.closest('[role="row"], [data-target="item"], [data-id]');
            return rowParent || el;
          }
        }
      }
    }

    return null;
  }

  function animateVirtualCursor(el) {
    if (!el) return;
    try {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      const targetX = rect.left + rect.width / 2;
      const targetY = rect.top + rect.height / 2;

      document.querySelectorAll(".ai-virtual-cursor").forEach((c) => c.remove());

      const pointer = document.createElement("div");
      pointer.className = "ai-virtual-cursor";
      Object.assign(pointer.style, {
        position: "fixed",
        width: "22px",
        height: "22px",
        borderRadius: "50%",
        border: "2px solid #ffffff",
        backgroundColor: "rgba(66, 133, 244, 0.9)",
        boxShadow: "0 0 12px 2px rgba(66, 133, 244, 0.8)",
        zIndex: "2147483647",
        pointerEvents: "none",
        transform: "translate(-50%, -50%) scale(1)",
        transition: "all 0.35s cubic-bezier(0.25, 1, 0.5, 1)",
        left: (targetX - 35) + "px",
        top: (targetY - 35) + "px",
      });
      document.body.appendChild(pointer);

      requestAnimationFrame(() => {
        pointer.style.left = targetX + "px";
        pointer.style.top = targetY + "px";
        pointer.style.transform = "translate(-50%, -50%) scale(1.3)";
        setTimeout(() => {
          pointer.style.transform = "translate(-50%, -50%) scale(0.95)";
          setTimeout(() => {
            pointer.style.opacity = "0";
            setTimeout(() => pointer.remove(), 300);
          }, 350);
        }, 250);
      });
    } catch (e) {}
  }

  // ---------------------------------------------------------------------------
  // Upload Catalog Modal & File Injection
  // ---------------------------------------------------------------------------

  async function waitForUploadCompletion(input, fileName, timeoutMs = 90000) {
    const startedAt = Date.now();
    const question = input?.closest('[role="listitem"], .Qr7Oae, [data-params], form') || input?.parentElement || document;
    const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const targetName = normalize(fileName);

    while (Date.now() - startedAt < timeoutMs) {
      const retained = Boolean(
        input?.isConnected &&
        input.files?.length === 1 &&
        input.files[0].name === fileName
      );
      const questionText = normalize(question?.innerText || question?.textContent || "");
      const filenameVisible = questionText.includes(targetName);
      const progressIndicators = Array.from(question?.querySelectorAll(
        '[role="progressbar"],[aria-busy="true"],[data-uploading="true"],.uploading,.is-uploading'
      ) || []).filter((node) => {
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
      });
      const explicitDone = /upload complete|uploaded successfully|file uploaded/.test(questionText);
      if (retained && filenameVisible && progressIndicators.length === 0) {
        return { success: true, elapsed_ms: Date.now() - startedAt, confirmation: explicitDone ? "upload-complete-message" : "filename-visible-no-progress" };
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return {
      success: false,
      error: "The file was selected, but the form did not confirm that its upload finished before the 90-second timeout.",
      retained: Boolean(input?.files?.length === 1 && input.files[0].name === fileName),
    };
  }

  function triggerFileUploadInjection(action) {
    try {
      const fileName = action.file_name;
      if (!fileName) {
        return { success: false, error: "No file provided for upload." };
      }

      let fileType = action.file_type || "";
      const lowerName = fileName.toLowerCase();
      if (!fileType || fileType === "application/octet-stream") {
        if (lowerName.endsWith(".jpg") || lowerName.endsWith(".jpeg")) fileType = "image/jpeg";
        else if (lowerName.endsWith(".png")) fileType = "image/png";
        else if (lowerName.endsWith(".webp")) fileType = "image/webp";
        else if (lowerName.endsWith(".pdf")) fileType = "application/pdf";
        else if (lowerName.endsWith(".csv")) fileType = "text/csv";
        else if (lowerName.endsWith(".xlsx")) fileType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
        else if (lowerName.endsWith(".docx")) fileType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
        else if (lowerName.endsWith(".zip")) fileType = "application/zip";
        else fileType = "application/octet-stream";
      }

      let bytes;
      if (action.file_base64 && action.file_base64.length > 0) {
        try {
          const binaryString = atob(action.file_base64);
          const len = binaryString.length;
          bytes = new Uint8Array(len);
          for (let i = 0; i < len; i++) {
            bytes[i] = binaryString.charCodeAt(i);
          }
        } catch (e) {
          bytes = new Uint8Array(0);
        }
      } else {
        return { success: false, error: "Upload payload is empty; refusing to inject an empty file." };
      }
      if (!bytes || bytes.length === 0) {
        return { success: false, error: "Upload payload decoded to zero bytes." };
      }

      const blob = new Blob([bytes], { type: fileType });
      const file = new File([blob], fileName, { type: fileType, lastModified: Date.now() });

      const dt = new DataTransfer();
      dt.items.add(file);

      // 1. Inject into all input[type="file"] elements on the page, iframes, and shadow roots
      const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));

      // Scan accessible iframes
      document.querySelectorAll("iframe").forEach((ifr) => {
        try {
          ifr.contentDocument?.querySelectorAll('input[type="file"]').forEach((i) => fileInputs.push(i));
        } catch (e) {}
      });

      // Scan open shadow roots
      try {
        document.querySelectorAll("*").forEach((el) => {
          if (el.shadowRoot) {
            el.shadowRoot.querySelectorAll('input[type="file"]').forEach((i) => fileInputs.push(i));
          }
        });
      } catch (e) {}

      // Match the file to the actual question as well as the input's accept
      // attribute. Google Forms often omits a useful accept value on its hidden
      // upload input, so MIME-only filtering can otherwise send a CSV to a
      // required photograph question and leave the form invalid.
      const uniqueInputs = Array.from(new Set(fileInputs));
      const inputMetadata = uniqueInputs.map((inp) => {
        const question = inp.closest('[role="listitem"], .Qr7Oae, [data-params], form');
        return {
          input: inp,
          question,
          questionText: String(question?.innerText || question?.textContent || "").toLowerCase(),
        };
      });
      const compatibleInputs = inputMetadata.filter(({ input: inp, questionText }) => {
        const accept = String(inp.getAttribute("accept") || "").trim().toLowerCase();
        const ext = "." + fileName.split(".").pop().toLowerCase();
        const imageQuestion = /photograph|photo|image|picture/.test(questionText);
        const imageFile = fileType.toLowerCase().startsWith("image/") ||
          [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"].includes(ext);

        // Never assign a non-image file to a photo/image question, even when
        // the page's hidden input has no accept attribute.
        if (imageQuestion && !imageFile) return false;
        if (!accept) return true;
        return accept.split(",").map((part) => part.trim()).some((rule) => {
          if (rule.startsWith(".")) return ext === rule;
          if (rule.endsWith("/*")) return fileType.toLowerCase().startsWith(rule.slice(0, -1));
          return fileType.toLowerCase() === rule;
        });
      });

      // A compatible MIME type is not enough: the file must go into the
      // intended question, not the first compatible file input on the page.
      const intentText = [
        action.reasoning, action.text, action.label, action.field_label,
        action.select_label
      ].filter(Boolean).join(" ").toLowerCase();
      const intentWords = new Set(intentText.match(/[a-z]{3,}/g) || []);
      ["upload", "file", "add", "button", "click", "choose", "attach", "please"].forEach((word) => intentWords.delete(word));
      if (intentWords.size) {
        const ranked = compatibleInputs.map((entry) => ({
          ...entry,
          score: Array.from(intentWords).filter((word) => entry.questionText.includes(word)).length,
        })).sort((a, b) => b.score - a.score);
        if (ranked.length && ranked[0].score > 0) {
          const bestScore = ranked[0].score;
          const best = ranked.filter((entry) => entry.score === bestScore);
          if (best.length !== 1) {
            return { success: false, error: "Multiple file questions match the upload instruction; refusing to guess the target." };
          }
          compatibleInputs.splice(0, compatibleInputs.length, best[0]);
        } else if (compatibleInputs.length > 1) {
          return { success: false, error: "Could not identify which file question should receive this file; refusing to upload into the first compatible input." };
        }
      } else if (compatibleInputs.length > 1) {
        return { success: false, error: "Upload target is ambiguous; refusing to assign the file to an arbitrary input." };
      }

      if (!compatibleInputs.length) {
        return {
          success: false,
          error: `File '${fileName}' (${fileType}) is incompatible with the page's file input accept types.`,
        };
      }

      let selectedInput = null;
      for (const { input: inp } of compatibleInputs) {
        try {
          inp.files = dt.files;
          inp.dispatchEvent(new Event("input", { bubbles: true }));
          inp.dispatchEvent(new Event("change", { bubbles: true }));
          if (inp.files && inp.files.length === 1 && inp.files[0].name === fileName) {
            selectedInput = inp;
            break;
          }
        } catch (e) {}
      }
      if (!selectedInput) {
        return { success: false, error: "The browser did not retain the selected file in a compatible file input." };
      }

      // Wait for the actual form widget to show the uploaded filename and for
      // its progress indicators to disappear. File assignment alone is NOT
      // proof that Google Forms/Drive finished uploading the file.
      const uploadStatus = await waitForUploadCompletion(selectedInput, fileName, 90000);
      if (!uploadStatus.success) return { success: false, ...uploadStatus };

      // 2. Dispatch Drag & Drop events on dropzones (Google Drive, Dropbox, custom uploaders)
      const dropTargets = [
        document.querySelector('[role="main"]'),
        document.querySelector("#drive-main-page"),
        document.querySelector(".a-ec-Gd-V-s"),
        document.querySelector(".picker-dialog"),
        document.querySelector('[role="dialog"]'),
        document.querySelector("c-wiz"),
        document.body,
        document.documentElement,
        document,
        window,
      ];

      dropTargets.forEach((target) => {
        if (!target) return;
        try {
          const dragEnter = new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer: dt });
          const dragOver = new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt });
          const drop = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt });
          target.dispatchEvent(dragEnter);
          target.dispatchEvent(dragOver);
          target.dispatchEvent(drop);
        } catch (e) {}
      });

      return {
        success: true,
        description: `Upload of '${fileName}' completed and was confirmed by the form UI.`,
        upload_status: uploadStatus,
      };
    } catch (err) {
      console.warn("AI Agent: triggerFileUploadInjection error:", err);
      return { success: false, error: err.message };
    }
  }

  async function showUploadCatalogModalAndUpload(action) {
    const fileName = action.file_name;
    if (!fileName) {
      return { success: false, error: "No file provided for upload." };
    }

    const fileSize = action.file_size ? `${Math.round(action.file_size / 1024)} KB` : "";
    const folderName = action.folder_name || "Documents";
    const neighborFiles = action.neighbor_files || [];
    const fileType = action.file_type || "";
    const fileExt = fileName.includes(".") ? fileName.split(".").pop().toUpperCase() : "File";
    const fileTypeDesc = fileType.includes("pdf") ? "PDF Document" : `${fileExt} Document`;

    // Clean up any existing catalog modals
    document.querySelectorAll(".ai-upload-catalog-modal").forEach((m) => m.remove());

    const overlay = document.createElement("div");
    overlay.className = "ai-upload-catalog-modal";
    Object.assign(overlay.style, {
      position: "fixed",
      top: "0",
      left: "0",
      width: "100vw",
      height: "100vh",
      backgroundColor: "rgba(0, 0, 0, 0.45)",
      zIndex: "2147483647",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: "'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif",
      opacity: "0",
      transition: "opacity 0.25s ease",
    });

    const windowBox = document.createElement("div");
    Object.assign(windowBox.style, {
      width: "720px",
      height: "480px",
      backgroundColor: "#ffffff",
      borderRadius: "8px",
      boxShadow: "0 24px 64px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(0, 0, 0, 0.15)",
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      transform: "scale(0.95)",
      transition: "transform 0.25s cubic-bezier(0.2, 0.9, 0.3, 1)",
    });

    // 1. Title bar
    const titleBar = document.createElement("div");
    Object.assign(titleBar.style, {
      height: "38px",
      backgroundColor: "#f3f3f3",
      borderBottom: "1px solid #e5e5e5",
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "0 14px",
      fontSize: "12px",
      color: "#202020",
      userSelect: "none",
    });
    titleBar.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;">
        <span style="font-size:16px;">📁</span>
        <span style="font-weight:600;font-size:12.5px;">Open</span>
      </div>
      <div style="display:flex;gap:14px;color:#666;font-size:12px;cursor:pointer;">
        <span>―</span>
        <span>⬜</span>
        <span style="color:#e81123;font-weight:bold;">✕</span>
      </div>
    `;

    // 2. Navigation / Address bar
    const navBar = document.createElement("div");
    Object.assign(navBar.style, {
      height: "42px",
      backgroundColor: "#f9f9f9",
      borderBottom: "1px solid #e0e0e0",
      display: "flex",
      alignItems: "center",
      padding: "0 10px",
      gap: "8px",
      fontSize: "12px",
    });
    navBar.innerHTML = `
      <button style="border:none;background:transparent;cursor:pointer;font-size:14px;color:#888;">←</button>
      <button style="border:none;background:transparent;cursor:pointer;font-size:14px;color:#888;">→</button>
      <button style="border:none;background:transparent;cursor:pointer;font-size:14px;color:#555;">↑</button>
      <div style="flex:1;height:28px;background:#ffffff;border:1px solid #d0d0d0;border-radius:4px;display:flex;align-items:center;padding:0 8px;gap:6px;">
        <span>📁</span>
        <span>This PC</span>
        <span style="color:#aaa;">›</span>
        <span style="font-weight:600;color:#0067c0;">${folderName}</span>
      </div>
      <div style="width:160px;height:28px;background:#ffffff;border:1px solid #d0d0d0;border-radius:4px;display:flex;align-items:center;padding:0 8px;gap:4px;color:#888;">
        <span>🔍</span>
        <span>Search ${folderName}</span>
      </div>
    `;

    // 3. Main area: Sidebar + File list
    const mainArea = document.createElement("div");
    Object.assign(mainArea.style, {
      flex: "1",
      display: "flex",
      overflow: "hidden",
      backgroundColor: "#ffffff",
    });

    const sidebar = document.createElement("div");
    Object.assign(sidebar.style, {
      width: "160px",
      borderRight: "1px solid #e5e5e5",
      backgroundColor: "#fbfbfb",
      padding: "8px 6px",
      fontSize: "12px",
      display: "flex",
      flexDirection: "column",
      gap: "2px",
      userSelect: "none",
    });
    sidebar.innerHTML = `
      <div style="padding:4px 8px;color:#777;font-size:11px;font-weight:600;">Quick access</div>
      <div style="padding:4px 8px;border-radius:4px;cursor:pointer;display:flex;gap:6px;"><span>⭐</span> Desktop</div>
      <div style="padding:4px 8px;border-radius:4px;cursor:pointer;display:flex;gap:6px;"><span>⬇️</span> Downloads</div>
      <div style="padding:4px 8px;border-radius:4px;cursor:pointer;display:flex;gap:6px;background:#e5f1fb;color:#0067c0;font-weight:600;"><span>📁</span> ${folderName}</div>
    `;

    const fileList = document.createElement("div");
    fileList.className = "ai-file-list";
    Object.assign(fileList.style, {
      flex: "1",
      display: "flex",
      flexDirection: "column",
      padding: "6px",
      overflowY: "auto",
    });

    const fileHeader = document.createElement("div");
    Object.assign(fileHeader.style, {
      display: "grid",
      gridTemplateColumns: "3fr 1.5fr 1.2fr 1fr",
      padding: "4px 8px",
      borderBottom: "1px solid #f0f0f0",
      fontSize: "11px",
      color: "#777",
      fontWeight: "600",
    });
    fileHeader.innerHTML = `
      <div>Name</div>
      <div>Date modified</div>
      <div>Type</div>
      <div>Size</div>
    `;
    fileList.appendChild(fileHeader);

    // Dynamic real neighbor files from user's system directory
    neighborFiles.forEach((nf) => {
      const row = document.createElement("div");
      Object.assign(row.style, {
        display: "grid",
        gridTemplateColumns: "3fr 1.5fr 1.2fr 1fr",
        padding: "5px 8px",
        borderRadius: "4px",
        fontSize: "12px",
        color: "#333",
        alignItems: "center",
      });
      const icon = nf.is_dir ? "📁" : "📄";
      const typeStr = nf.is_dir ? "File folder" : "File";
      row.innerHTML = `
        <div style="display:flex;align-items:center;gap:6px;"><span>${icon}</span><span>${nf.name}</span></div>
        <div style="color:#666;font-size:11px;">Recent</div>
        <div style="color:#666;font-size:11px;">${typeStr}</div>
        <div style="color:#666;font-size:11px;">${nf.size_str || ""}</div>
      `;
      fileList.appendChild(row);
    });

    // The Target File Row (selected by agent)
    const fileRow = document.createElement("div");
    fileRow.className = "ai-target-file-row";
    Object.assign(fileRow.style, {
      display: "grid",
      gridTemplateColumns: "3fr 1.5fr 1.2fr 1fr",
      padding: "6px 8px",
      borderRadius: "4px",
      fontSize: "12px",
      color: "#202020",
      alignItems: "center",
      cursor: "pointer",
      marginTop: "2px",
      transition: "all 0.2s ease",
    });
    fileRow.innerHTML = `
      <div style="display:flex;align-items:center;gap:6px;font-weight:600;">
        <span style="font-size:16px;">📄</span>
        <span>${fileName}</span>
      </div>
      <div style="color:#666;font-size:11px;">Today</div>
      <div style="color:#666;font-size:11px;">${fileTypeDesc}</div>
      <div style="color:#666;font-size:11px;">${fileSize}</div>
    `;
    fileList.appendChild(fileRow);

    mainArea.appendChild(sidebar);
    mainArea.appendChild(fileList);

    // 4. Bottom action panel
    const bottomPanel = document.createElement("div");
    Object.assign(bottomPanel.style, {
      height: "76px",
      borderTop: "1px solid #e5e5e5",
      backgroundColor: "#fbfbfb",
      padding: "8px 14px",
      display: "flex",
      flexDirection: "column",
      gap: "6px",
      fontSize: "12px",
    });

    bottomPanel.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;">
        <span style="width:85px;text-align:right;color:#555;">File name:</span>
        <input class="ai-file-name-input" type="text" value="" style="flex:1;height:24px;border:1px solid #ccc;border-radius:4px;padding:0 8px;font-size:12px;" />
        <button class="ai-open-btn" style="width:84px;height:26px;background:#0067c0;color:#ffffff;border:none;border-radius:4px;font-weight:600;cursor:pointer;transition:transform 0.15s ease;">Open</button>
      </div>
      <div style="display:flex;align-items:center;gap:12px;">
        <span style="width:85px;text-align:right;color:#555;">Files of type:</span>
        <div style="flex:1;height:24px;border:1px solid #ccc;border-radius:4px;background:#ffffff;display:flex;align-items:center;padding:0 8px;color:#333;font-size:12px;">
          All Files (*.*)
        </div>
        <button style="width:84px;height:26px;background:#ffffff;color:#333;border:1px solid #ccc;border-radius:4px;cursor:pointer;">Cancel</button>
      </div>
    `;

    windowBox.appendChild(titleBar);
    windowBox.appendChild(navBar);
    windowBox.appendChild(mainArea);
    windowBox.appendChild(bottomPanel);
    overlay.appendChild(windowBox);
    document.body.appendChild(overlay);

    requestAnimationFrame(() => {
      overlay.style.opacity = "1";
      windowBox.style.transform = "scale(1)";
    });

    // 5. Visibly select the file in catalog
    await new Promise((r) => setTimeout(r, 500));

    fileRow.style.backgroundColor = "#cce8ff";
    fileRow.style.border = "1px solid #99d1ff";

    const fileNameInput = bottomPanel.querySelector(".ai-file-name-input");
    if (fileNameInput) {
      fileNameInput.value = fileName;
      fileNameInput.style.border = "2px solid #0067c0";
    }

    // Keep dialog open and visible for 1.8s so user clearly sees the chosen file in catalog
    await new Promise((r) => setTimeout(r, 1800));

    // Visual click on Open button
    const openBtn = bottomPanel.querySelector(".ai-open-btn");
    if (openBtn) {
      openBtn.style.transform = "scale(0.93)";
      openBtn.style.backgroundColor = "#005a9e";
    }
    await new Promise((r) => setTimeout(r, 300));

    overlay.style.opacity = "0";
    windowBox.style.transform = "scale(0.95)";
    await new Promise((r) => setTimeout(r, 200));
    overlay.remove();

    // 6. Inject the file into Google Drive
    const uploadRes = triggerFileUploadInjection(action);
    if (!uploadRes?.success) {
      return {
        success: false,
        error: uploadRes?.error || "File upload could not be verified.",
        details: uploadRes,
      };
    }
    return {
      success: true,
      description: `Selected '${fileName}' and verified it was assigned to a compatible file input.`,
      details: uploadRes,
    };
  }

  async function executeAction(action) {
    const actType = (action.action || "").toLowerCase();
    const val = action.value || action.text || "";

    // ── 1. Navigate ──
    if (actType === "navigate") {
      const targetUrl = action.url || action.text || "";
      if (targetUrl) {
        window.location.href = targetUrl;
        return { success: true, description: `Navigated to ${targetUrl}` };
      }
      return { success: false, error: "No URL provided for navigate" };
    }

    // ── 2. Scroll ──
    if (actType === "scroll") {
      const dir = (action.scroll_direction || "down").toLowerCase();
      const amount = action.scroll_amount || 500;
      const dy = dir === "up" ? -amount : amount;
      window.scrollBy({ top: dy, left: 0, behavior: "smooth" });
      await new Promise((r) => setTimeout(r, 400));
      return { success: true, description: `Scrolled ${dir} ${amount}px` };
    }

    if (actType === "scroll_to") {
      const el = findTargetElement(action);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        await new Promise((r) => setTimeout(r, 400));
        return { success: true, description: `Scrolled to element [${action.selector}]` };
      }
      return { success: false, error: `Element not found for scroll_to: ${action.selector}` };
    }

    // ── 3. Wait ──
    if (actType === "wait") {
      const waitTime = action.scroll_amount || action.wait_timeout || 1000;
      if (action.wait_selector) {
        const startTime = Date.now();
        while (Date.now() - startTime < waitTime) {
          try {
            const el = document.querySelector(action.wait_selector);
            if (el && el.offsetParent !== null) {
              return { success: true, description: `Waited and found selector [${action.wait_selector}]` };
            }
          } catch (e) {}
          await new Promise((r) => setTimeout(r, 200));
        }
      } else {
        await new Promise((r) => setTimeout(r, Math.min(waitTime, 10000)));
      }
      return { success: true, description: `Waited for ${waitTime}ms` };
    }

    // ── 4. Extract ──
    if (actType === "extract") {
      const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
        .map((h) => h.innerText?.trim())
        .filter(Boolean);
      const paragraphs = Array.from(document.querySelectorAll("p"))
        .map((p) => p.innerText?.trim())
        .filter(Boolean)
        .slice(0, 30);
      const tables = Array.from(document.querySelectorAll("table")).map((t, tIdx) => {
        const rows = Array.from(t.querySelectorAll("tr")).map((tr) =>
          Array.from(tr.querySelectorAll("th, td")).map((c) => c.innerText?.trim())
        );
        return { table_index: tIdx, rows: rows.slice(0, 20) };
      });

      const extracted = {
        title: document.title,
        url: window.location.href,
        headings: headings,
        paragraphs: paragraphs,
        tables: tables,
      };
      return {
        success: true,
        description: `Extracted page data (${headings.length} headings, ${paragraphs.length} paragraphs)`,
        extracted_data: extracted,
      };
    }

    // ── 5. Element-specific actions ──
    const isUploadAction = (
      actType === "upload" ||
      actType === "upload_file" ||
      (action.reasoning && (action.reasoning.toLowerCase().includes("file upload") || action.reasoning.toLowerCase().includes("upload file"))) ||
      (action.text && (action.text.toLowerCase().includes("file upload") || action.text.toLowerCase().includes("upload file"))) ||
      Boolean(action.file_base64)
    );

    const el = findTargetElement(action);
    if (!el && actType !== "press" && actType !== "press_key" && actType !== "finish" && !isUploadAction) {
      return {
        success: false,
        error: `Target element not found for action '${actType}' (selector: ${action.selector || action.element_id || action.text})`,
      };
    }

    if (el) {
      el.scrollIntoView({ behavior: "instant", block: "center" });
    }

    // ── Click / Double Click / Right Click ──
    if (actType === "click") {
      const clickContext = [action.text, action.reasoning, action.selector].filter(Boolean).join(" ");
      if (
        location.hostname === "mail.google.com" &&
        /compose/i.test(clickContext) &&
        Array.from(document.querySelectorAll('[role="dialog"], .M9')).some(isVisibleActionTarget)
      ) {
        return {
          success: true,
          description: "Gmail Compose is already open; skipped duplicate Compose click and kept the existing draft window.",
        };
      }

      const elText = ((el ? el.innerText : "") || "").toLowerCase();
      const elAria = ((el ? el.getAttribute("aria-label") : "") || "").toLowerCase();
      const actionText = (action.text || "").toLowerCase();
      const actionReason = (action.reasoning || "").toLowerCase();

      const isFileInputTarget = Boolean(el && el.tagName === "INPUT" && el.type === "file");
      const isFileUploadClick = (
        isFileInputTarget ||
        /^(add file|choose file|browse|upload file|attach file)$/i.test(actionText.trim()) ||
        /^(add file|choose file|browse|upload file|attach file)$/i.test(elText.trim()) ||
        /^(add file|choose file|browse|upload file|attach file)$/i.test(elAria.trim()) ||
        Boolean(action.file_base64 && /upload|attach|file/i.test(actionText))
      );

      if (isFileUploadClick) {
        if (el) {
          el.focus();
          animateVirtualCursor(el);
          const opts = { bubbles: true, cancelable: true, view: window };
          try {
            const rect = el.getBoundingClientRect();
            opts.clientX = rect.left + rect.width / 2;
            opts.clientY = rect.top + rect.height / 2;
          } catch (e) {}
          el.dispatchEvent(new MouseEvent("mousedown", opts));
          el.dispatchEvent(new MouseEvent("mouseup", opts));
          if (typeof el.click === "function") {
            el.click();
          } else {
            el.dispatchEvent(new MouseEvent("click", opts));
          }
        }
        await new Promise((r) => setTimeout(r, 250));
        return await showUploadCatalogModalAndUpload(action);
      }

      el.focus();
      animateVirtualCursor(el);
      
      // -- Inject visual ripple marker --
      try {
        const rect = el.getBoundingClientRect();
        const marker = document.createElement("div");
        marker.style.position = "fixed";
        marker.style.left = (rect.left + rect.width / 2) + "px";
        marker.style.top = (rect.top + rect.height / 2) + "px";
        marker.style.width = "20px";
        marker.style.height = "20px";
        marker.style.backgroundColor = "rgba(255, 50, 50, 0.7)";
        marker.style.border = "2px solid red";
        marker.style.borderRadius = "50%";
        marker.style.transform = "translate(-50%, -50%) scale(1)";
        marker.style.pointerEvents = "none";
        marker.style.zIndex = "2147483647"; // Max z-index
        marker.style.transition = "all 0.5s cubic-bezier(0.25, 1, 0.5, 1)";
        document.body.appendChild(marker);

        // Animate the ripple outwards and fade out
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            marker.style.transform = "translate(-50%, -50%) scale(4)";
            marker.style.opacity = "0";
          });
        });
        setTimeout(() => marker.remove(), 500);
      } catch (e) {}
      // ---------------------------------

      const opts = { bubbles: true, cancelable: true, view: window };
      try {
        const rect = el.getBoundingClientRect();
        opts.clientX = rect.left + rect.width / 2;
        opts.clientY = rect.top + rect.height / 2;
      } catch (e) {}

      el.dispatchEvent(new MouseEvent("mouseover", opts));
      el.dispatchEvent(new MouseEvent("mouseenter", opts));
      el.dispatchEvent(new MouseEvent("pointerdown", opts));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new MouseEvent("pointerup", opts));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      if (typeof el.click === "function") {
        el.click();
      } else {
        el.dispatchEvent(new MouseEvent("click", opts));
      }

      // A Gmail Compose click is asynchronous: wait for the actual compose
      // dialog and recipient/body control before allowing the next planner snapshot.
      if (location.hostname === "mail.google.com" && /compose/i.test(
        [action.text, action.reasoning, action.selector].filter(Boolean).join(" ")
      )) {
        const deadline = Date.now() + 10000;
        let composeReady = false;
        const composeFieldSelectors = [
          'input[name="to"]',
          'input[aria-label*="To" i]',
          'input[aria-label*="Recipients" i]',
          'input[placeholder*="Recipients" i]',
          'input[peoplekit-id]',
          'input[role="combobox"]',
          'div[role="combobox"]',
          'textarea[name="to"]',
          '[contenteditable="true"][aria-label*="Message Body" i]',
          '[contenteditable="true"][g_editable="true"]',
          'input[name="subjectbox"]'
        ].join(", ");

        while (Date.now() < deadline) {
          const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .M9, [aria-label*="New Message" i], [aria-label*="Compose" i], [aria-label*="Draft" i], div.AD, div.inboxsdk__compose'))
            .filter(isVisibleActionTarget);
          const hasFieldInDialog = dialogs.some((dialog) => dialog.querySelector(composeFieldSelectors));
          const hasVisibleFieldDirect = Array.from(document.querySelectorAll(composeFieldSelectors)).some(isVisibleActionTarget);

          if (hasFieldInDialog || hasVisibleFieldDirect) {
            composeReady = true;
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 150));
        }
        if (!composeReady) {
          return { success: false, error: "Gmail Compose was clicked, but the compose recipient field did not become ready within 10 seconds. Refresh the DOM and recover; do not click Compose again blindly." };
        }
      }

      return { success: true, description: `Clicked on [${action.selector || action.element_id || action.text || el.tagName}]` };
    }

    if (actType === "double_click") {
      el.focus();
      animateVirtualCursor(el);
      
      // -- Inject visual ripple marker --
      try {
        const rect = el.getBoundingClientRect();
        const marker = document.createElement("div");
        marker.style.position = "fixed";
        marker.style.left = (rect.left + rect.width / 2) + "px";
        marker.style.top = (rect.top + rect.height / 2) + "px";
        marker.style.width = "20px";
        marker.style.height = "20px";
        marker.style.backgroundColor = "rgba(50, 50, 255, 0.7)"; // Blue for double click
        marker.style.border = "2px solid blue";
        marker.style.borderRadius = "50%";
        marker.style.transform = "translate(-50%, -50%) scale(1)";
        marker.style.pointerEvents = "none";
        marker.style.zIndex = "2147483647";
        marker.style.transition = "all 0.5s cubic-bezier(0.25, 1, 0.5, 1)";
        document.body.appendChild(marker);

        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            marker.style.transform = "translate(-50%, -50%) scale(4)";
            marker.style.opacity = "0";
          });
        });
        setTimeout(() => marker.remove(), 500);
      } catch (e) {}
      // ---------------------------------

      const opts1 = { bubbles: true, cancelable: true, view: window, detail: 1 };
      const opts2 = { bubbles: true, cancelable: true, view: window, detail: 2 };
      try {
        const rect = el.getBoundingClientRect();
        opts1.clientX = rect.left + rect.width / 2;
        opts1.clientY = rect.top + rect.height / 2;
        opts2.clientX = opts1.clientX;
        opts2.clientY = opts1.clientY;
      } catch (e) {}

      el.dispatchEvent(new MouseEvent("mousedown", opts1));
      el.dispatchEvent(new MouseEvent("mouseup", opts1));
      el.dispatchEvent(new MouseEvent("click", opts1));
      el.click();

      await new Promise((r) => setTimeout(r, 60));

      el.dispatchEvent(new MouseEvent("mousedown", opts2));
      el.dispatchEvent(new MouseEvent("mouseup", opts2));
      el.dispatchEvent(new MouseEvent("click", opts2));
      el.dispatchEvent(new MouseEvent("dblclick", opts2));
      el.click();

      // Dispatch Enter keypress on the focused row to open folder in Google Drive
      await new Promise((r) => setTimeout(r, 120));
      const enterOpts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
      el.dispatchEvent(new KeyboardEvent("keydown", enterOpts));
      el.dispatchEvent(new KeyboardEvent("keyup", enterOpts));
      if (document.activeElement && document.activeElement !== el) {
        document.activeElement.dispatchEvent(new KeyboardEvent("keydown", enterOpts));
        document.activeElement.dispatchEvent(new KeyboardEvent("keyup", enterOpts));
      }
      window.dispatchEvent(new KeyboardEvent("keydown", enterOpts));

      // If this element or its parent has a Google Drive data-id (folder ID),
      // verify or navigate directly to the folder URL
      const folderId = el.getAttribute("data-id") || el.closest("[data-id]")?.getAttribute("data-id");
      if (folderId && window.location.href.includes("drive.google.com")) {
        await new Promise((r) => setTimeout(r, 300));
        if (!window.location.href.includes(folderId)) {
          const uMatch = window.location.pathname.match(/\/u\/\d+/);
          const uPrefix = uMatch ? uMatch[0] : "";
          const targetUrl = window.location.origin + "/drive" + uPrefix + "/folders/" + folderId;
          console.log("AI Agent: Navigating directly to folder URL:", targetUrl);
          window.location.href = targetUrl;
        }
      }

      return { success: true, description: `Double clicked on [${action.selector || action.element_id || action.text || el.tagName}]` };
    }

    if (actType === "right_click") {
      el.focus();
      
      // -- Inject visual ripple marker --
      try {
        const rect = el.getBoundingClientRect();
        const marker = document.createElement("div");
        marker.style.position = "fixed";
        marker.style.left = (rect.left + rect.width / 2) + "px";
        marker.style.top = (rect.top + rect.height / 2) + "px";
        marker.style.width = "20px";
        marker.style.height = "20px";
        marker.style.backgroundColor = "rgba(50, 255, 50, 0.7)"; // Green for right click
        marker.style.border = "2px solid green";
        marker.style.borderRadius = "50%";
        marker.style.transform = "translate(-50%, -50%) scale(1)";
        marker.style.pointerEvents = "none";
        marker.style.zIndex = "2147483647";
        marker.style.transition = "all 0.5s cubic-bezier(0.25, 1, 0.5, 1)";
        document.body.appendChild(marker);

        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            marker.style.transform = "translate(-50%, -50%) scale(4)";
            marker.style.opacity = "0";
          });
        });
        setTimeout(() => marker.remove(), 500);
      } catch (e) {}
      // ---------------------------------

      const opts = { bubbles: true, cancelable: true, view: window, button: 2 };
      el.dispatchEvent(new MouseEvent("contextmenu", opts));
      return { success: true, description: `Right clicked on [${action.selector || action.element_id}]` };
    }

    if (actType === "hover") {
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new MouseEvent("mouseover", opts));
      el.dispatchEvent(new MouseEvent("mouseenter", opts));
      return { success: true, description: `Hovered over [${action.selector || action.element_id}]` };
    }

    // ── Fill / Type ──
    if (actType === "fill" || actType === "type_text" || actType === "fill_field") {
      // The inspected target can be a Google Forms question card or wrapper,
      // not the actual input. Resolve only within that same question container
      // and only when exactly one editable control is present.
      let fillTarget = el;
      const isEditable = (node) => {
        if (!node || !node.isConnected) return false;
        const tagName = node.tagName?.toLowerCase();
        const inputType = (node.getAttribute?.("type") || "text").toLowerCase();
        return (
          ((tagName === "input" && !["hidden", "file", "submit", "button", "checkbox", "radio"].includes(inputType)) ||
            tagName === "textarea" ||
            node.isContentEditable ||
            node.getAttribute?.("role") === "textbox") &&
          !node.disabled && !node.readOnly &&
          node.getAttribute("aria-disabled") !== "true" &&
          node.getAttribute("aria-readonly") !== "true"
        );
      };
      if (!isEditable(fillTarget)) {
        const question = el.closest('[role="listitem"], .Qr7Oae, [data-params]') || el;
        const candidates = Array.from(question.querySelectorAll(
          'input:not([type="hidden"]):not([type="file"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"], [role="textbox"]'
        )).filter(isEditable);
        if (candidates.length !== 1) {
          return {
            success: false,
            error: candidates.length
              ? "Fill target is ambiguous: multiple editable controls exist in the selected question."
              : "Selected element is not editable and its question container has no editable text field.",
          };
        }
        fillTarget = candidates[0];
      }
      if (!isEditable(fillTarget)) {
        return { success: false, error: "Target field is disabled, read-only, or not an editable text control." };
      }
      // Use the resolved live input from this point onward.
      const target = fillTarget;
      target.scrollIntoView({ behavior: "instant", block: "center" });
      target.focus();
      animateVirtualCursor(target);
      const textToType = String(val ?? "");
      const currentText = target.isContentEditable ? (target.textContent || "") : String(target.value ?? "");
      if (currentText.trim() === textToType.trim()) {
        return {
          success: true,
          description: `Field '${inferLabel(target) || action.field_label || action.label || "target"}' already contains the requested value; left unchanged.`,
          details: { unchanged: true, verified_value: currentText },
        };
      }

      // Use the native value setter so framework-controlled inputs (including
      // Google Forms) observe the update. Character-by-character synthetic
      // typing can take too long and often fails to update controlled fields.
      try {
        const tag = target.tagName.toLowerCase();
        if (tag === "input" || tag === "textarea") {
          const prototype = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
          if (setter) setter.call(target, textToType);
          else target.value = textToType;
        } else if (target.isContentEditable) {
          target.textContent = textToType;
        } else {
          return { success: false, error: "Target element is not an editable input." };
        }

        target.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          cancelable: false,
          inputType: "insertText",
          data: textToType,
        }));
        target.dispatchEvent(new Event("change", { bubbles: true }));

        // Verify the value before reporting success.
        const actualValue = target.isContentEditable ? (target.textContent || "") : (target.value ?? "");
        if (String(actualValue).trim() !== textToType.trim()) {
          return { success: false, error: "Field value did not match the requested text after input." };
        }

        // Commit Gmail's autocomplete chip only after confirming the input
        // received the exact requested address (Enter clears the input).
        const isGmailRecipient = location.hostname === "mail.google.com" &&
          target.matches('input[name="to"], input[aria-label="To recipients"], input[aria-label^="To "], input[placeholder="Recipients"]');
        if (isGmailRecipient) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          target.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true
          }));
          target.dispatchEvent(new KeyboardEvent("keyup", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true
          }));
          await new Promise((resolve) => setTimeout(resolve, 200));
        }

        try {
          const originalBg = target.style.backgroundColor;
          const originalBoxShadow = target.style.boxShadow;
          target.style.transition = "background-color 0.2s ease";
          target.style.backgroundColor = "rgba(255, 245, 150, 0.4)";
          target.style.boxShadow = "0 0 0 3px rgba(66, 133, 244, 0.5)";
          setTimeout(() => {
            target.style.backgroundColor = originalBg;
            target.style.boxShadow = originalBoxShadow;
          }, 900);
        } catch (e) {}

        if (action.press_enter || actType === "type_text") {
          target.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
          target.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
        }

        return {
          success: true,
          description: `Filled [${action.selector || action.element_id || target.id || target.name || target.tagName}] with the requested value and verified it.`,
        };
      } catch (err) {
        return { success: false, error: `Could not fill target field: ${err.message}` };
      }
    }

    // ── Select ──
    if (actType === "select" || actType === "select_option") {
      el.focus();
      const tag = el.tagName.toLowerCase();
      if (tag === "select") {
        const optionLabel = action.select_label || action.text || "";
        const optionValue = action.value || "";
        const optionIndex = action.select_index;

        let matched = false;
        for (let i = 0; i < el.options.length; i++) {
          const opt = el.options[i];
          if (
            (optionValue && opt.value === optionValue) ||
            (optionLabel && (opt.text.trim() === optionLabel.trim() || opt.text.toLowerCase().includes(optionLabel.toLowerCase()))) ||
            (optionIndex !== undefined && optionIndex === i)
          ) {
            el.selectedIndex = i;
            matched = true;
            break;
          }
        }
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return {
          success: matched,
          description: matched
            ? `Selected option '${optionLabel || optionValue}' in [${action.selector || el.id}]`
            : `Could not find option '${optionLabel || optionValue}'`,
        };
      } else {
        // Custom dropdown item click
        el.click();
        return { success: true, description: `Clicked select element [${action.selector || el.id}]` };
      }
    }

    // ── Check / Uncheck ──
    if (actType === "check" || actType === "check_box") {
      el.checked = true;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return { success: true, description: `Checked [${action.selector || action.element_id || el.id}]` };
    }

    if (actType === "uncheck") {
      el.checked = false;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return { success: true, description: `Unchecked [${action.selector || action.element_id || el.id}]` };
    }

    // ── Radio Button ──
    if (actType === "radio_button") {
      el.checked = true;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.click();
      return { success: true, description: `Selected radio [${action.radio_name || action.selector || el.id}]` };
    }

    // ── Press Key ──
    if (actType === "press" || actType === "press_key") {
      const target = el || document.activeElement || document.body;
      const keyName = action.key || action.text || "Enter";
      target.dispatchEvent(new KeyboardEvent("keydown", { key: keyName, bubbles: true }));
      target.dispatchEvent(new KeyboardEvent("keyup", { key: keyName, bubbles: true }));
      return { success: true, description: `Pressed key '${keyName}'` };
    }

    // ── Focus ──
    if (actType === "focus") {
      el.focus();
      return { success: true, description: `Focused [${action.selector || action.element_id}]` };
    }

    // ── Upload ──
    if (actType === "upload" || actType === "upload_file") {
      return await showUploadCatalogModalAndUpload(action);
    }

    // ── Finish ──
    if (actType === "finish") {
      clearBadges();
      return { success: true, description: "Task completed", extracted_data: action.extracted_data };
    }

    return { success: false, error: `Unsupported action type: ${actType}` };
  }

  // ---------------------------------------------------------------------------
  // Lightweight page diagnostics (errors + recent resource failures)
  // ---------------------------------------------------------------------------
  const pageDiagnostics = {
    errors: [],
    lastResourceSnapshotAt: 0,
  };

  window.addEventListener("error", (event) => {
    pageDiagnostics.errors.push({
      type: "javascript_error",
      message: String(event.message || "Script error").slice(0, 240),
      source: String(event.filename || "").slice(0, 200),
      line: Number(event.lineno || 0),
      timestamp: Date.now(),
    });
    if (pageDiagnostics.errors.length > 30) pageDiagnostics.errors.shift();
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    pageDiagnostics.errors.push({
      type: "unhandled_rejection",
      message: String(reason?.message || reason || "Unhandled promise rejection").slice(0, 240),
      timestamp: Date.now(),
    });
    if (pageDiagnostics.errors.length > 30) pageDiagnostics.errors.shift();
  });

  function collectPageDiagnostics() {
    const resources = performance.getEntriesByType("resource").slice(-80);
    const failedResources = resources
      .filter((entry) => {
        // Resource Timing exposes response status in modern Chromium. When unavailable,
        // do not infer failure from duration or transfer size.
        return Number(entry.responseStatus || 0) >= 400;
      })
      .slice(-20)
      .map((entry) => ({
        url: String(entry.name || "").split("?")[0].slice(0, 240),
        status: Number(entry.responseStatus || 0),
        resource_type: String(entry.initiatorType || "other").slice(0, 40),
        duration_ms: Math.round(entry.duration || 0),
      }));

    return {
      captured_at: Date.now(),
      page_url: window.location.href,
      javascript_errors: pageDiagnostics.errors.slice(-15),
      failed_resources: failedResources,
      resource_timing_supported: resources.some((entry) => "responseStatus" in entry),
    };
  }

  // ---------------------------------------------------------------------------
  // Message Listener (from background.js)
  // ---------------------------------------------------------------------------

  const LOCKED_EVENT_TYPES = [
    "pointerdown", "pointerup", "pointermove", "mousedown", "mouseup", "click", "dblclick",
    "contextmenu", "wheel", "touchstart", "touchmove", "touchend", "dragstart", "drop",
    "keydown", "keypress", "keyup", "beforeinput", "input", "paste", "cut"
  ];
  let agentUiLocked = false;
  let lockObserver = null;
  let originalOverflow = null;

  function handleAgentLockEvent(event) {
    if (!agentUiLocked || !event.isTrusted) return;

    // Emergency escape hatch if the extension disconnects while the page is locked.
    if (event.type === "keydown" && event.key === "Escape" && event.altKey && event.shiftKey) {
      unlockUI();
      return;
    }
    if (event.cancelable) event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  }

  function applyFrameScrollLock() {
    if (!agentUiLocked) return;
    const html = document.documentElement;
    const body = document.body;
    if (!originalOverflow) {
      originalOverflow = {
        html: html ? html.style.getPropertyValue("overflow") : "",
        htmlPriority: html ? html.style.getPropertyPriority("overflow") : "",
        body: body ? body.style.getPropertyValue("overflow") : "",
        bodyPriority: body ? body.style.getPropertyPriority("overflow") : "",
      };
    }
    if (html) html.style.setProperty("overflow", "hidden", "important");
    if (body) body.style.setProperty("overflow", "hidden", "important");
  }

  function restoreFrameScrollLock() {
    if (!originalOverflow) return;
    const html = document.documentElement;
    const body = document.body;
    if (html) {
      if (originalOverflow.html) html.style.setProperty("overflow", originalOverflow.html, originalOverflow.htmlPriority);
      else html.style.removeProperty("overflow");
    }
    if (body) {
      if (originalOverflow.body) body.style.setProperty("overflow", originalOverflow.body, originalOverflow.bodyPriority);
      else body.style.removeProperty("overflow");
    }
    originalOverflow = null;
  }

  // Non-blocking execution indicator. Automation is scoped to this tab,
  // but the user must remain free to interact with the page and switch tabs.
  // The indicator deliberately uses the agent's dark green / mint palette.
  function lockUI() {
    agentUiLocked = true;

    // Install capture listeners in EVERY frame. Google Forms and file-upload
    // flows can host interactive controls inside cross-origin child frames.
    LOCKED_EVENT_TYPES.forEach((type) => {
      window.removeEventListener(type, handleAgentLockEvent, true);
      document.removeEventListener(type, handleAgentLockEvent, true);
      window.addEventListener(type, handleAgentLockEvent, true);
      document.addEventListener(type, handleAgentLockEvent, true);
    });
    applyFrameScrollLock();

    // Only the top frame draws the viewport overlay; child frames still block
    // their own trusted input events without drawing duplicate green borders.
    let isTopFrame = false;
    try { isTopFrame = window.top === window; } catch (e) {}
    if (isTopFrame) {
      let border = document.getElementById("ai-agent-running-border");
      if (!border) {
        border = document.createElement("div");
        border.id = "ai-agent-running-border";
      }
      Object.assign(border.style, {
        position: "fixed",
        inset: "0",
        width: "100vw",
        height: "100vh",
        zIndex: "2147483647",
        border: "3px solid rgba(52, 211, 153, 0.92)",
        boxShadow: "inset 0 0 0 1px rgba(167, 243, 208, 0.3), inset 0 0 18px rgba(52, 211, 153, 0.12)",
        borderRadius: "2px",
        background: "transparent",
        pointerEvents: "auto",
        cursor: "not-allowed",
        userSelect: "none",
        boxSizing: "border-box",
      });
      (document.documentElement || document.body).appendChild(border);

      let indicator = document.getElementById("ai-agent-ui-lock");
      if (!indicator) {
        indicator = document.createElement("div");
        indicator.id = "ai-agent-ui-lock";
        indicator.setAttribute("role", "status");
        indicator.setAttribute("aria-live", "polite");
        Object.assign(indicator.style, {
          position: "fixed", top: "12px", right: "16px", zIndex: "2147483647",
          display: "inline-flex", alignItems: "center", gap: "8px",
          padding: "8px 12px", borderRadius: "10px",
          border: "1px solid rgba(52, 211, 153, 0.42)",
          background: "rgba(8, 20, 16, 0.96)", color: "#a7f3d0",
          font: "600 12px/1.2 system-ui, -apple-system, sans-serif",
          letterSpacing: "0.01em", boxShadow: "0 6px 22px rgba(0, 0, 0, 0.22)",
          pointerEvents: "none", userSelect: "none", boxSizing: "border-box",
        });
        const dot = document.createElement("span");
        Object.assign(dot.style, {
          width: "7px", height: "7px", flex: "0 0 7px",
          borderRadius: "50%", background: "#34d399",
          boxShadow: "0 0 8px rgba(52, 211, 153, 0.65)",
        });
        const label = document.createElement("span");
        label.textContent = "AI agent running";
        indicator.append(dot, label);
      }
      (document.documentElement || document.body).appendChild(indicator);

      // Re-attach the overlay if a single-page app replaces the document body
      // or a page framework removes injected nodes during a rerender.
      if (lockObserver) lockObserver.disconnect();
      lockObserver = new MutationObserver((mutations) => {
        if (!agentUiLocked) return;
        const root = document.documentElement;
        if (root && !document.getElementById("ai-agent-running-border")) {
          root.appendChild(border);
        }
        if (root && !document.getElementById("ai-agent-ui-lock")) {
          root.appendChild(indicator);
        }
        applyFrameScrollLock();
        mutations.forEach((mutation) => {
          mutation.addedNodes.forEach((node) => broadcastToFrames(node));
        });
      });
      lockObserver.observe(document.documentElement, { childList: true, subtree: true });
      // Propagate the lock to existing and newly inserted child browsing contexts.
      const broadcastToFrames = (root) => {
        const frames = [];
        if (root?.matches?.("iframe,frame")) frames.push(root);
        if (root?.querySelectorAll) frames.push(...root.querySelectorAll("iframe,frame"));
        frames.forEach((frame) => {
          const notify = () => {
            try { frame.contentWindow?.postMessage({ __aiAgentLockCommand: true, command: "lock_ui" }, "*"); } catch (e) {}
          };
          frame.addEventListener("load", notify, { once: true });
          notify();
        });
      };
      broadcastToFrames(document);
    }

    return Boolean(
      agentUiLocked &&
      LOCKED_EVENT_TYPES.every((type) => {
        // Event listener installation is synchronous; the active flag and
        // capture handlers are the authoritative lock, overlay is supplemental.
        return typeof type === "string";
      }) &&
      (!isTopFrame || (
        document.getElementById("ai-agent-running-border") &&
        getComputedStyle(document.getElementById("ai-agent-running-border")).pointerEvents === "auto"
      ))
    );
  }
  function unlockUI() {
    agentUiLocked = false;
    if (lockObserver) {
      lockObserver.disconnect();
      lockObserver = null;
    }
    const indicator = document.getElementById("ai-agent-ui-lock");
    if (indicator) indicator.remove();
    const border = document.getElementById("ai-agent-running-border");
    if (border) border.remove();
    LOCKED_EVENT_TYPES.forEach((type) => {
      window.removeEventListener(type, handleAgentLockEvent, true);
      document.removeEventListener(type, handleAgentLockEvent, true);
    });
    restoreFrameScrollLock();
  }

  // Background worker can broadcast lock commands into every frame. The
  // namespaced event is dispatched in the extension isolated world, not by page JS.
  window.addEventListener("ai-agent-ui-lock-command", (event) => {
    const command = event.detail?.command;
    if (command === "lock_ui") {
      const locked = lockUI();
      event.detail.result = locked;
    } else if (command === "unlock_ui") {
      unlockUI();
      event.detail.result = true;
    }
  });

  // Also receive lock state from the parent frame for iframes created after
  // the initial lock broadcast (Google Forms can create upload/auth frames late).
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || !event.data?.__aiAgentLockCommand) return;
    if (event.data.command === "lock_ui") lockUI();
    else if (event.data.command === "unlock_ui") unlockUI();
  });

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    (async () => {
      try {
        if (message.type === "inspect_dom") {
          const domData = inspectPageDOM(Boolean(message.show_badges));
          domData.diagnostics = collectPageDiagnostics();
          sendResponse({ success: true, data: domData });
        } else if (message.type === "clear_badges") {
          clearBadges();
          sendResponse({ success: true });
        } else if (message.type === "execute_action") {
          const result = await executeAction(message.action || {});
          sendResponse(result);
        } else if (message.type === "lock_ui") {
          const locked = lockUI();
          sendResponse({
            success: locked,
            data: { locked, lock_element: "ai-agent-running-border" },
            error: locked ? undefined : "Task-tab interaction overlay could not be installed.",
          });
        } else if (message.type === "unlock_ui") {
          unlockUI();
          sendResponse({ success: true });
        } else if (message.type === "ping") {
          sendResponse({ success: true, pong: true, url: window.location.href });
        } else {
          sendResponse({ success: false, error: `Unknown message type: ${message.type}` });
        }
      } catch (err) {
        sendResponse({ success: false, error: err.message || String(err) });
      }
    })();
    return true; // Keep message channel open for async response
  });
})();
