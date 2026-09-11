// popup.js — Управление интерфейсом расширения MRKT Token Grabber

document.addEventListener('DOMContentLoaded', () => {
  const toggleRecordBtn = document.getElementById('toggleRecordBtn');
  const recordBtnText = document.getElementById('recordBtnText');
  const statusBadge = document.getElementById('statusBadge');
  const statusMessage = document.getElementById('statusMessage');
  const tokenCount = document.getElementById('tokenCount');
  const tokensArea = document.getElementById('tokensArea');
  const copyBtn = document.getElementById('copyBtn');
  const downloadBtn = document.getElementById('downloadBtn');
  const clearBtn = document.getElementById('clearBtn');
  const toast = document.getElementById('toast');

  let currentRecordingState = false;
  let currentTokens = [];

  // Загрузка начального состояния
  chrome.storage.local.get(['isRecording', 'tokens'], (data) => {
    currentRecordingState = !!data.isRecording;
    currentTokens = data.tokens || [];
    updateView();
  });

  // Отслеживание изменений из фонового скрипта в реальном времени
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local') {
      if (changes.isRecording !== undefined) {
        currentRecordingState = changes.isRecording.newValue;
      }
      if (changes.tokens !== undefined) {
        currentTokens = changes.tokens.newValue || [];
      }
      updateView();
    }
  });

  // Обновление интерфейса
  function updateView() {
    // Состояние кнопки и статуса
    if (currentRecordingState) {
      toggleRecordBtn.className = 'btn btn-stop';
      recordBtnText.textContent = 'Остановить запись';
      toggleRecordBtn.querySelector('.btn-icon').textContent = '⏹';

      statusBadge.textContent = 'ЗАПИСЬ';
      statusBadge.className = 'badge badge-recording';

      statusMessage.innerHTML = '🔴 <b>Идёт перехват!</b> Переключайте аккаунты в MRKT — новые токены сразу попадают в список.';
    } else {
      toggleRecordBtn.className = 'btn btn-record';
      recordBtnText.textContent = 'Начать запись';
      toggleRecordBtn.querySelector('.btn-icon').textContent = '⏺';

      statusBadge.textContent = 'ОЖИДАНИЕ';
      statusBadge.className = 'badge badge-idle';

      statusMessage.innerHTML = 'Нажмите <b>«Начать запись»</b>, затем откройте <b>tgmrkt.io</b> под каждым аккаунтом.';
    }

    // Список и счётчик
    tokenCount.textContent = currentTokens.length;
    tokensArea.value = currentTokens.join('\n');
  }

  // Переключение режима записи
  toggleRecordBtn.addEventListener('click', () => {
    const newState = !currentRecordingState;
    chrome.storage.local.set({ isRecording: newState }, () => {
      currentRecordingState = newState;
      updateView();
    });
  });

  // Копирование списка в буфер обмена
  copyBtn.addEventListener('click', async () => {
    const text = tokensArea.value.trim();
    if (!text) {
      showToast('⚠️ Список пуст!', '#f59e0b');
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
      showToast('✅ Скопировано в буфер!', '#10b981');
    } catch (err) {
      tokensArea.select();
      document.execCommand('copy');
      showToast('✅ Скопировано в буфер!', '#10b981');
    }
  });

  // Скачивание готового файла tokens.txt
  downloadBtn.addEventListener('click', () => {
    const text = tokensArea.value.trim();
    if (!text) {
      showToast('⚠️ Список пуст!', '#f59e0b');
      return;
    }

    const blob = new Blob([text + '\n'], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tokens.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast('💾 Файл tokens.txt скачан!', '#3b82f6');
  });

  // Очистка списка токенов
  clearBtn.addEventListener('click', () => {
    if (currentTokens.length === 0) return;
    if (confirm(`Очистить список из ${currentTokens.length} токенов?`)) {
      chrome.storage.local.set({ tokens: [] }, () => {
        currentTokens = [];
        updateView();
        showToast('🗑 Список очищен', '#ef4444');
      });
    }
  });

  // Всплывающее уведомление
  let toastTimer = null;
  function showToast(msg, bgColor = '#10b981') {
    toast.textContent = msg;
    toast.style.background = bgColor;
    toast.classList.remove('hidden');

    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.classList.add('hidden');
    }, 2200);
  }
});
