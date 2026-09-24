// Roda no contexto (MAIN world) da própria página do Reclame Aqui — por isso
// consegue sobrescrever window.fetch / XMLHttpRequest antes da página usá-los.
(function () {
  const TOKEN_URL_HINT = "/auth/token";

  // Só mexe no corpo se parecer uma chamada de LOGIN (tem username/password),
  // não numa chamada de refresh_token — pra não arriscar quebrar o refresh.
  function tryInjectOfflineScope(bodyStr, contentType) {
    try {
      if (contentType && contentType.includes("application/json")) {
        const obj = JSON.parse(bodyStr);
        if (obj && (obj.password !== undefined || obj.username !== undefined)) {
          if (!obj.scope) {
            obj.scope = "openid offline_access";
          } else if (!String(obj.scope).includes("offline_access")) {
            obj.scope = obj.scope + " offline_access";
          }
          return JSON.stringify(obj);
        }
      } else {
        const params = new URLSearchParams(bodyStr);
        if (params.has("password") || params.has("username")) {
          const scope = params.get("scope");
          if (!scope) params.set("scope", "openid offline_access");
          else if (!scope.includes("offline_access")) params.set("scope", scope + " offline_access");
          return params.toString();
        }
      }
    } catch (e) {
      console.warn("[RA Token Capturer] não consegui ajustar o body da requisição:", e);
    }
    return bodyStr;
  }

  function reportTokens(payload) {
    window.postMessage({ source: "ra-token-capturer", payload }, "*");
  }

  // --- fetch ---
  const originalFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    let finalInit = init;

    if (url && url.includes(TOKEN_URL_HINT) && init && init.body) {
      const headers = init.headers || {};
      const contentType =
        (typeof headers.get === "function" ? headers.get("Content-Type") : headers["Content-Type"] || headers["content-type"]) ||
        "application/json";
      finalInit = { ...init, body: tryInjectOfflineScope(init.body, contentType) };
    }

    const response = await originalFetch(input, finalInit);

    if (url && url.includes(TOKEN_URL_HINT)) {
      response
        .clone()
        .json()
        .then((json) => {
          if (json && (json.access_token || json.refresh_token)) {
            reportTokens(json);
          }
        })
        .catch(() => {});
    }

    return response;
  };

  // --- XMLHttpRequest (caso o login use XHR em vez de fetch) ---
  const XHR = window.XMLHttpRequest;
  const originalOpen = XHR.prototype.open;
  const originalSend = XHR.prototype.send;
  const originalSetHeader = XHR.prototype.setRequestHeader;

  XHR.prototype.open = function (method, url, ...rest) {
    this.__ra_url = url;
    this.__ra_headers = {};
    return originalOpen.call(this, method, url, ...rest);
  };

  XHR.prototype.setRequestHeader = function (name, value) {
    this.__ra_headers = this.__ra_headers || {};
    this.__ra_headers[String(name).toLowerCase()] = value;
    return originalSetHeader.call(this, name, value);
  };

  XHR.prototype.send = function (body) {
    if (this.__ra_url && this.__ra_url.includes(TOKEN_URL_HINT) && body) {
      const contentType = (this.__ra_headers && this.__ra_headers["content-type"]) || "application/json";
      body = tryInjectOfflineScope(body, contentType);
      this.addEventListener("load", function () {
        try {
          const json = JSON.parse(this.responseText);
          if (json && (json.access_token || json.refresh_token)) {
            reportTokens(json);
          }
        } catch (e) {}
      });
    }
    return originalSend.call(this, body);
  };
})();
