// background.js — Service worker для перехвата токенов MRKT

const UUID_REGEX = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;

// Инициализация при установке
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(['tokens', 'isRecording'], (result) => {
    if (!result.tokens) {
      chrome.storage.local.set({ tokens: [] });
    }
    if (result.isRecording === undefined) {
      chrome.storage.local.set({ isRecording: false });
    }
    updateBadge(result.isRecording || false, (result.tokens || []).length);
  });
});

// Обновление бейджа на иконке расширения
function updateBadge(isRecording, count) {
  if (isRecording) {
    chrome.action.setBadgeText({ text: count > 0 ? String(count) : 'REC' });
    chrome.action.setBadgeBackgroundColor({ color: '#10b981' }); // Зелёный
  } else {
    chrome.action.setBadgeText({ text: count > 0 ? String(count) : '' });
    chrome.action.setBadgeBackgroundColor({ color: '#6b7280' }); // Серый
  }
}

// Слушатель изменения хранилища для синхронизации бейджа
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local') {
    chrome.storage.local.get(['tokens', 'isRecording'], (data) => {
      updateBadge(data.isRecording || false, (data.tokens || []).length);
    });
  }
});

// Перехват исходящих заголовков запросов к tgmrkt.io
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    chrome.storage.local.get(['isRecording', 'tokens'], (data) => {
      if (!data.isRecording) return;

      const headers = details.requestHeaders || [];
      const candidateTokens = new Set();

      for (const h of headers) {
        const name = (h.name || '').toLowerCase();
        const val = h.value || '';

        // 1. Заголовок Authorization: <UUID> или Bearer <UUID>
        if (name === 'authorization') {
          const matches = val.match(UUID_REGEX);
          if (matches) {
            matches.forEach((t) => candidateTokens.add(t.toLowerCase()));
          }
        }

        // 2. Cookie: access_token=<UUID>
        if (name === 'cookie') {
          const cookieMatch = val.match(/access_token=([0-9a-fA-F-]{36})/i);
          if (cookieMatch && UUID_REGEX.test(cookieMatch[1])) {
            candidateTokens.add(cookieMatch[1].toLowerCase());
          }
          // Также ищем любые UUID внутри cookie
          const allMatches = val.match(UUID_REGEX);
          if (allMatches) {
            allMatches.forEach((t) => candidateTokens.add(t.toLowerCase()));
          }
        }
      }

      // 3. Проверка параметров URL
      try {
        const url = new URL(details.url);
        const urlMatches = url.search.match(UUID_REGEX);
        if (urlMatches) {
          urlMatches.forEach((t) => candidateTokens.add(t.toLowerCase()));
        }
      } catch (e) {}

      if (candidateTokens.size === 0) return;

      const currentTokens = data.tokens || [];
      let addedCount = 0;
      let lastAddedToken = '';

      for (const token of candidateTokens) {
        if (!currentTokens.includes(token)) {
          currentTokens.push(token);
          lastAddedToken = token;
          addedCount++;
        }
      }

      if (addedCount > 0) {
        chrome.storage.local.set({ tokens: currentTokens }, () => {
          updateBadge(true, currentTokens.length);

          // Уведомление пользователя о новом токене
          try {
            chrome.notifications.create({
              type: 'basic',
              iconUrl: 'icons/icon128.png',
              title: '🎯 Токен MRKT перехвачен!',
              message: `Аккаунт #${currentTokens.length}: ${lastAddedToken.substring(0, 8)}... (${addedCount} новых)`,
              priority: 2,
            });
          } catch (e) {
            console.log('Notification error:', e);
          }
        });
      }
    });
  },
  {
    urls: ['*://*.tgmrkt.io/*'],
  },
  ['requestHeaders', 'extraHeaders']
);
