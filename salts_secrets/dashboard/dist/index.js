// Salt's Secrets dashboard tab: the owner board (Context, Permissions, Guests, Doc) rendered locally from this
// plugin's own API, in the board's own colours and bundled fonts. Each page runs in a same-origin frame so its layout and scripts stay intact; its inline
// scripts become blob: scripts because the browser-hosted dashboard forbids inline script.
(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const React = SDK.React;
  const { useState, useEffect, useRef } = SDK.hooks;
  const h = React.createElement;
  const API = "/api/plugins/salts-secrets";
  const ASSETS = (window.__HERMES_BASE_PATH__ || "") + "/dashboard-plugins/salts-secrets";

  function withBlobScripts(html, urls) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script:not([src])").forEach((inline) => {
      const url = URL.createObjectURL(new Blob([inline.textContent], { type: "text/javascript" }));
      urls.push(url);
      const script = doc.createElement("script");
      script.src = url;
      inline.replaceWith(script);
    });
    return "<!doctype html>" + doc.documentElement.outerHTML;
  }

  function useFillHeight(ref) {
    const [height, setHeight] = useState(800);
    useEffect(() => {
      const fit = () => ref.current && setHeight(Math.max(480, window.innerHeight - ref.current.getBoundingClientRect().top - 12));
      fit();
      window.addEventListener("resize", fit);
      return () => window.removeEventListener("resize", fit);
    }, []);
    return height;
  }

  function Board() {
    const [page, setPage] = useState("shelf");
    const [doc, setDoc] = useState(null);
    const [error, setError] = useState("");
    const box = useRef(null);
    const height = useFillHeight(box);

    useEffect(() => {
      window.__saltsSecretsNav = setPage;
      return () => { delete window.__saltsSecretsNav; };
    }, []);

    useEffect(() => {
      const urls = [];
      let live = true;
      setError("");
      SDK.fetchJSON(API + "/page/" + page)
        .then((r) => live && setDoc(withBlobScripts(r.html.split("__SALT_ASSETS__").join(ASSETS), urls)))
        .catch((e) => live && setError(String((e && e.message) || e)));
      return () => {
        live = false;
        setTimeout(() => urls.forEach((u) => URL.revokeObjectURL(u)), 30000);
      };
    }, [page]);

    return h("div", { className: "salts-secrets-board", ref: box },
      error
        ? h("p", { className: "salts-secrets-error" }, "Could not load this page: " + error)
        : doc && h("iframe", { key: page, srcDoc: doc, title: "Salt's Secrets", style: { height: height + "px" } }));
  }

  window.__HERMES_PLUGINS__.register("salts-secrets", Board);
})();
