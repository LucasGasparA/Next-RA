// Roda no contexto isolado padrão da extensão — tem acesso a chrome.runtime,
// mas não pode ver variáveis da página. Por isso content-main.js manda os
// dados via postMessage, e este script só repassa pro background.
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "ra-token-capturer") return;

  chrome.runtime.sendMessage({ type: "ra-tokens-captured", payload: data.payload });
});
