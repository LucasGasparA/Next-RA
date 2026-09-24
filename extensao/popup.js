function render(record) {
  const el = document.getElementById("content");

  if (!record || !record.refresh_token) {
    el.innerHTML = '<p class="empty">Nenhum token capturado ainda. Faça login como empresa no Reclame Aqui (com a extensão ativa) e volte aqui.</p>';
    return;
  }

  el.innerHTML = `
    <div class="field">
      <label>refresh_token (cole em refresh_token.txt)</label>
      <textarea id="rt" readonly>${record.refresh_token}</textarea>
      <button id="copyRt">Copiar</button>
      <button id="downloadRt">Baixar refresh_token.txt</button>
    </div>
    <div class="field">
      <label>access_token</label>
      <textarea id="at" readonly>${record.access_token || ""}</textarea>
      <button id="copyAt">Copiar</button>
    </div>
    <div class="ts">Capturado em ${new Date(record.captured_at).toLocaleString("pt-BR")}</div>
    <p class="warn">Confira no DevTools (aba Network) se a chamada pra /auth/token continua retornando 200 depois de instalar a extensão — ela injeta o scope offline_access no corpo da requisição de login.</p>
  `;

  document.getElementById("copyRt").onclick = () => navigator.clipboard.writeText(record.refresh_token);
  document.getElementById("copyAt").onclick = () => navigator.clipboard.writeText(record.access_token || "");
  document.getElementById("downloadRt").onclick = () => {
    const blob = new Blob([record.refresh_token], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    chrome.downloads.download({ url, filename: "refresh_token.txt", saveAs: true });
  };
}

chrome.storage.local.get("ra_last_tokens", (data) => render(data.ra_last_tokens));

chrome.storage.onChanged.addListener((changes) => {
  if (changes.ra_last_tokens) render(changes.ra_last_tokens.newValue);
});
