(() => {
  const initialNode = document.getElementById('initial-storage-summary');
  if (!initialNode) return;

  const feedback = document.getElementById('stats-feedback');
  const refreshButton = document.getElementById('refresh-storage-stats');
  const applyButton = document.getElementById('apply-stats-prefix');
  const prefixInput = document.getElementById('stats-prefix');
  const typeList = document.getElementById('stats-type-list');
  let summary = JSON.parse(initialNode.textContent || '{}');

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));

  const formatBytes = (bytes) => {
    const numeric = Number(bytes) || 0;
    if (numeric <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let value = numeric;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  };

  const formatTime = (value) => {
    if (!value) return '-';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', {hour12: false});
  };

  const showFeedback = (message, type = 'success') => {
    feedback.textContent = message;
    feedback.className = `alert ${type === 'error' ? 'alert-error' : 'alert-success'}`;
  };

  const render = () => {
    document.getElementById('stats-total-size').textContent = formatBytes(summary.total_bytes);
    document.getElementById('stats-object-count').textContent = Number(summary.object_count) || 0;
    document.getElementById('stats-prefix-count').textContent = Number(summary.object_count) || 0;
    document.getElementById('stats-prefix-label').textContent = summary.prefix || 'bucket 根目录';
    document.getElementById('stats-recent-size').textContent = formatBytes(summary.recent_7d_bytes);
    document.getElementById('stats-recent-count').textContent = `${Number(summary.recent_7d_object_count) || 0} 个对象`;
    document.getElementById('stats-refreshed-at').textContent = summary.refreshed_at ? `刷新于 ${formatTime(summary.refreshed_at)}` : '';
    typeList.innerHTML = (summary.type_distribution || []).map((item) => `
      <div class="stats-type-row">
        <div class="stats-type-copy"><strong>${escapeHtml(item.label)}</strong><span>${Number(item.count) || 0} 个 · ${formatBytes(item.bytes)}</span></div>
        <div class="stats-type-meter" aria-label="${escapeHtml(item.label)} ${Number(item.percent) || 0}%"><span style="width:${Math.max(0, Math.min(100, Number(item.percent) || 0))}%"></span></div>
        <strong class="stats-type-percent">${(Number(item.percent) || 0).toFixed(1)}%</strong>
      </div>`).join('');
  };

  const refresh = async () => {
    refreshButton.disabled = true;
    applyButton.disabled = true;
    const prefix = prefixInput.value.trim();
    try {
      const response = await fetch(`/api/stats/refresh?prefix=${encodeURIComponent(prefix)}`, {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败 (${response.status})`);
      summary = payload.summary || {};
      prefixInput.value = summary.prefix || '';
      const url = new URL(window.location.href);
      if (summary.prefix) url.searchParams.set('prefix', summary.prefix);
      else url.searchParams.delete('prefix');
      window.history.replaceState({}, '', url);
      render();
      showFeedback(payload.message || '存储统计已刷新');
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      refreshButton.disabled = false;
      applyButton.disabled = false;
    }
  };

  refreshButton.addEventListener('click', refresh);
  applyButton.addEventListener('click', refresh);
  prefixInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      refresh();
    }
  });
  render();
})();
