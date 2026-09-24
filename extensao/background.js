chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "ra-tokens-captured") {
    const payload = msg.payload || {};
    const record = {
      access_token: payload.access_token || null,
      refresh_token: payload.refresh_token || null,
      expires_in: payload.expires_in || null,
      captured_at: new Date().toISOString(),
    };

    chrome.storage.local.set({ ra_last_tokens: record }, () => {
      chrome.action.setBadgeText({ text: record.refresh_token ? "OK" : "" });
      chrome.action.setBadgeBackgroundColor({ color: "#45B8A6" });
    });
  }
});
