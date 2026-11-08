(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const { useState, useEffect, useCallback } = React;
  const h = React.createElement;
  const API = "/api/plugins/salts-secrets";

  const get = (path) => SDK.fetchJSON(API + path);
  const post = (path, body) => SDK.fetchJSON(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

  function useLoad(path) {
    const [state, setState] = useState({ data: null, error: null });
    const reload = useCallback(() => {
      get(path).then((data) => setState({ data, error: null }), (error) => setState({ data: null, error }));
    }, [path]);
    useEffect(reload, [reload]);
    return [state, reload];
  }

  function Status({ state, children }) {
    if (state.error) return h("p", { className: "ss-error" }, String(state.error.message || state.error));
    if (!state.data) return h("p", { className: "ss-muted" }, "Loading…");
    return children(state.data);
  }

  const TIER_LABEL = { A: "A · as-is", B: "B · rewrite", C: "C · private" };

  function ContextTab() {
    const [state, reload] = useLoad("/items");
    const [picked, setPicked] = useState(null);
    const [texts, setTexts] = useState({});
    const [open, setOpen] = useState(null);
    const [result, setResult] = useState(null);
    const [cls, setCls] = useState({});
    const [via, setVia] = useState({});
    const [busy, setBusy] = useState("");

    useEffect(() => {
      if (!state.data) return;
      setPicked(new Set(state.data.items.filter((i) => i.status === "public").map((i) => i.id)));
      setTexts(Object.fromEntries(state.data.items.filter((i) => i.public_text).map((i) => [i.id, i.public_text])));
      setCls(Object.fromEntries(state.data.items.filter((i) => i.classification).map((i) => [i.id, i.classification])));
      setVia({});
    }, [state.data]);

    const runClassify = useCallback(() => {
      setBusy("Classifying…");
      post("/classify", {}).then((r) => { setCls(r.items || {}); setBusy(r.model ? "" : "No model configured: rules only, so nothing is A."); },
        (e) => setBusy("Classify failed: " + String(e.message || e)));
    }, []);
    useEffect(() => { if (state.data) runClassify(); }, [state.data && state.data.items.length]);

    const mark = (ids, how) => setVia((prev) => { const next = { ...prev }; ids.forEach((id) => (next[id] = how)); return next; });

    const toggle = (id) => {
      mark([id], "single");
      setPicked((prev) => {
        const next = new Set(prev);
        next.has(id) ? next.delete(id) : next.add(id);
        return next;
      });
    };

    const show = (item) => {
      if (open && open.id === item.id) return setOpen(null);
      get("/items/" + encodeURIComponent(item.id)).then((r) => setOpen({ id: item.id, text: r.text }));
    };

    const tierIds = (tier) => state.data.items.filter((i) => i.publishable && cls[i.id] && cls[i.id].tier === tier).map((i) => i.id);

    const bulk = async (tier) => {
      const ids = tierIds(tier);
      mark(ids, "bulk-" + tier);
      if (tier === "C") return setPicked((prev) => new Set([...prev].filter((id) => !ids.includes(id))));
      if (tier === "A") {
        setTexts((prev) => { const next = { ...prev }; ids.forEach((id) => delete next[id]); return next; });
        return setPicked((prev) => new Set([...prev, ...ids]));
      }
      let done = 0;
      const drafted = {};
      for (const id of ids) {
        setBusy(`Drafting ${++done}/${ids.length}…`);
        try { drafted[id] = (await post("/draft", { id })).draft || ""; } catch (e) { drafted[id] = ""; }
      }
      setTexts((prev) => ({ ...prev, ...drafted }));
      setPicked((prev) => new Set([...prev, ...ids.filter((id) => drafted[id])]));
      setBusy(`${ids.filter((id) => !drafted[id]).length} B entries had nothing safe to share and stay private.`);
    };

    const publish = () => post("/publish", {
      public_ids: [...picked].filter((id) => !(via[id] === "bulk-B" && !(texts[id] || "").trim())),
      public_texts: texts,
      decisions: Object.fromEntries(Object.entries(via).map(([id, how]) => [id, { via: how }])),
    }).then((r) => { setResult(r); reload(); }, (e) => setResult({ error: String(e.message || e) }));

    return h(Status, { state }, (data) => {
      const groups = {};
      data.items.forEach((item) => (groups[item.group] = groups[item.group] || []).push(item));
      const n = { A: tierIds("A").length, B: tierIds("B").length, C: tierIds("C").length };
      return h("div", null,
        h("p", { className: "ss-lead" },
          "Everything below is private by default. Tick what strangers may know about ", data.owner_name,
          "; the public profile only ever sees the ticked items."),
        h("div", { className: "ss-triage" },
          h("p", { className: "ss-muted" }, "Rules first, then your model: A crosses unchanged, B only as a guest version, C stays private. ",
            "A bulk button only takes entries of its own tier and C never crosses in bulk; every click goes to the approval log."),
          h("div", { className: "ss-actions" },
            h("button", { onClick: runClassify }, "Classify"),
            h("button", { disabled: !n.A, onClick: () => bulk("A") }, `A · adopt all ${n.A} as-is`),
            h("button", { disabled: !n.B, onClick: () => bulk("B") }, `B · draft guest versions for all ${n.B}`),
            h("button", { disabled: !n.C, onClick: () => bulk("C") }, `C · keep all ${n.C} private`),
            busy && h("span", { className: "ss-muted" }, busy))),
        Object.entries(groups).map(([group, items]) => h("section", { key: group, className: "ss-group" },
          h("h3", null, group),
          items.map((item) => h("div", { key: item.id, className: "ss-item ss-" + item.status },
            h("label", null,
              h("input", {
                type: "checkbox", disabled: !item.publishable,
                checked: !!(picked && picked.has(item.id)), onChange: () => toggle(item.id),
              }),
              h("span", { className: "ss-title" }, item.title),
              h("span", { className: "ss-badge" }, item.publishable ? item.status : "never shared"),
              cls[item.id] && h("span", { className: "ss-badge ss-tier-" + cls[item.id].tier }, TIER_LABEL[cls[item.id].tier])),
            cls[item.id] && h("div", { className: "ss-muted ss-why" }, cls[item.id].reason,
              (cls[item.id].rules_hit || []).length ? " · rules: " + cls[item.id].rules_hit.join(", ") : "",
              via[item.id] || item.decided_via ? " · " + (via[item.id] || item.decided_via) : ""),
            h("button", { className: "ss-link", onClick: () => show(item) }, open && open.id === item.id ? "hide" : "view"),
            open && open.id === item.id && h("pre", { className: "ss-text" }, open.text),
            picked && picked.has(item.id) && h("textarea", {
              className: "ss-rewrite", placeholder: "Optional: what guests see instead of the original",
              value: texts[item.id] || "", onChange: (e) => { mark([item.id], "single"); setTexts({ ...texts, [item.id]: e.target.value }); },
            }))))),
        h("div", { className: "ss-actions" },
          h("button", { className: "ss-primary", onClick: publish, disabled: !picked }, "Publish to guests"),
          result && (result.error
            ? h("span", { className: "ss-error" }, result.error)
            : h("span", { className: "ss-muted" },
              `${result.facts} facts and ${result.documents} documents public` +
              (result.revoked ? `; ${result.revoked} revoked, ${result.deleted} guest sessions purged` : "") +
              (result.failed ? `; REVOKE INCOMPLETE: ${result.failed} guest session(s) not deleted, their guests now get fresh sessions; publish again to retry` : "") +
              ((result.rejected || []).length ? `; ${result.rejected.length} refused (bulk click outside its tier)` : "") +
              (result.combination && result.combination.hit ? "; " + result.combination.message : "")))));
    });
  }

  function PermissionsTab() {
    const [state, reload] = useLoad("/permissions");
    const [guest, setGuest] = useState(null);
    useEffect(() => { if (state.data) setGuest(new Set(state.data.guest)); }, [state.data]);
    const toggle = (id) => setGuest((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
    return h(Status, { state }, (data) => h("div", null,
      h("p", { className: "ss-lead" },
        "Toolsets the public profile may use. Locked toolsets touch your machine or memory and are never offered to guests."),
      h("table", { className: "ss-table" }, h("tbody", null, data.catalog.map((t) => h("tr", { key: t.id, className: t.locked ? "ss-locked" : "" },
        h("td", null, h("input", {
          type: "checkbox", disabled: t.locked, checked: !!(guest && guest.has(t.id)), onChange: () => toggle(t.id),
        })),
        h("td", null, h("strong", null, t.label), h("div", { className: "ss-muted" }, t.tools.join(", "))),
        h("td", { className: "ss-muted" }, t.locked_reason || t.warning || ""))))),
      h("div", { className: "ss-actions" },
        h("button", { className: "ss-primary", disabled: !guest, onClick: () => post("/permissions", { guest: [...guest] }).then(reload) },
          "Save"))));
  }

  function GuestsTab() {
    const [state, reload] = useLoad("/guests");
    return h(Status, { state }, (data) => h("div", null,
      h("p", { className: "ss-lead" },
        `${data.counts.sessions} guest sessions, ${data.counts.questions} questions, ${data.counts.probes} looked like probes.`),
      data.sessions.length === 0 && h("p", { className: "ss-muted" }, "No guest has talked to the public profile yet."),
      data.sessions.map((s) => h("section", { key: s.id, className: "ss-group" },
        h("h3", null, new Date((s.started_at || 0) * 1000).toLocaleString(), " · ", s.source || "api", s.live ? "" : " · archived"),
        s.turns.map((turn, i) => h("div", { key: i, className: "ss-turn ss-" + turn.role },
          h("strong", null, turn.role === "user" ? "guest" : "public profile"), " ", turn.text)))),
      h("div", { className: "ss-actions" },
        h("button", { onClick: () => post("/guests/clear").then(reload) }, "Archive and clear guest sessions"))));
  }

  function CheckTab() {
    const [state, reload] = useLoad("/check");
    return h(Status, { state }, (data) => h("div", null,
      h("p", { className: data.ok ? "ss-ok ss-headline" : "ss-error ss-headline" }, data.line),
      h("ul", { className: "ss-checks" }, data.checks.map((c, i) => h("li", { key: i, className: c.ok ? "ss-ok" : "ss-error" },
        (c.ok ? "✓ " : "✗ ") + c.text))),
      h("div", { className: "ss-actions" }, h("button", { onClick: reload }, "Run again"))));
  }

  const TABS = [["Context", ContextTab], ["Permissions", PermissionsTab], ["Guests", GuestsTab], ["Check", CheckTab]];

  function Page() {
    const [tab, setTab] = useState(0);
    return h("div", { className: "ss-page" },
      h("header", null,
        h("h1", null, "Salt's Secrets"),
        h("p", { className: "ss-muted" }, "One Hermes, two minds: your DM reaches you, everyone else reaches the public profile.")),
      h("nav", { className: "ss-tabs" }, TABS.map(([label], i) =>
        h("button", { key: label, className: i === tab ? "ss-tab ss-active" : "ss-tab", onClick: () => setTab(i) }, label))),
      h(TABS[tab][1]));
  }

  window.__HERMES_PLUGINS__.register("salts-secrets", Page);
})();
