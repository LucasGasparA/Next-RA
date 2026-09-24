# RA Token Capturer (extensão Chromium)

Captura automaticamente o `refresh_token` (com `scope: openid offline_access`)
quando você faz login como empresa no Reclame Aqui — sem precisar montar a
requisição no Postman/DevTools na mão.

## Como funciona

Um content script injetado no contexto da própria página sobrescreve
`window.fetch` e `XMLHttpRequest` só para chamadas que batem com
`/auth/token`. Quando detecta uma chamada de **login** (corpo com
`username`/`password`), adiciona `scope: "openid offline_access"` antes de
enviar. Quando a resposta volta, captura `access_token`/`refresh_token` e
manda pro popup da extensão via `chrome.storage.local`.

⚠️ **Antes de confiar nela no dia a dia**: abra o DevTools (aba Network) no
site do Reclame Aqui, faça login com a extensão ativa, e confirme que a
chamada pra `/auth/token` retorna `200` normalmente. Eu não tenho acesso à
tela de login real pra validar o formato exato do corpo da requisição —
a extensão foi escrita pra mexer só no campo `scope` e não tocar em mais
nada, mas vale confirmar antes de usar pra valer.

## Instalar (modo desenvolvedor)

1. Abra `chrome://extensions` (funciona igual no Edge/Brave: `edge://extensions`, `brave://extensions`).
2. Ative o **Modo do desenvolvedor** (canto superior direito).
3. Clique em **Carregar sem compactação** e selecione esta pasta (`extension/`).
4. Fixe o ícone da extensão na barra do navegador (ícone de peça de quebra-cabeça → pin).

## Usar

1. Com a extensão instalada, acesse o Reclame Aqui e faça login normalmente
   como empresa (com o login/senha e recaptcha de sempre — a extensão não
   muda a tela de login, só intercepta a chamada por trás).
2. Clique no ícone da extensão. Se tudo deu certo, você verá o
   `refresh_token` já pronto, com botão **Baixar refresh_token.txt**.
3. Salve esse arquivo na mesma pasta do `main.py` (substitui o arquivo
   antigo, se existir).

## Se não funcionar

- Confira no DevTools se a URL da chamada de login realmente contém
  `/auth/token`. Se o Reclame Aqui usar outra URL (ex: um subdomínio
  diferente), me manda a URL exata que eu ajusto `TOKEN_URL_HINT` em
  `content-main.js`.
- Se a resposta não trouxer `refresh_token` mesmo depois da extensão
  injetar o `scope`, pode ser que o backend do RA exija esse scope
  configurado de outra forma (ex: só aceito se o client OAuth já estiver
  cadastrado com esse scope liberado) — nesse caso o caminho manual via
  Postman (README principal) ainda é o fallback confiável.
