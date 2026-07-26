(() => {
  const list = document.getElementById('share-list');
  if (!list) return;

  const feedback = document.getElementById('share-feedback');
  const refreshButton = document.getElementById('refresh-shares');
  const openButton = document.getElementById('open-share-form');
  const closeButton = document.getElementById('close-share-form');
  const createPanel = document.getElementById('share-create-panel');
  const createForm = document.getElementById('share-create-form');
  const createButton = document.getElementById('create-share-submit');
  const createdLink = document.getElementById('share-created-link');
  const createdUrl = document.getElementById('share-created-url');
  const copyButton = document.getElementById('copy-share-url');
  const filterButtons = Array.from(document.querySelectorAll('[data-share-filter]'));
  const count = document.getElementById('share-count');
  let shares = [];
  let activeFilter = 'all';

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', ...(options.headers || {})},
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败 (${response.status})`);
    return payload;
  };

  const showFeedback = (message, type = 'success') => {
    feedback.textContent = message;
    feedback.className = `alert ${type === 'error' ? 'alert-error' : 'alert-success'}`;
  };

  const formatTime = (value) => {
    if (!value) return '-';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', {hour12: false});
  };

  const statusLabel = (status) => ({
    active: '有效', expired: '已过期', revoked: '已撤销', exhausted: '次数用完',
  }[status] || status || '-');

  const matchesFilter = (share) => {
    if (activeFilter === 'all') return true;
    if (activeFilter === 'active') return share.status === 'active';
    return share.status !== 'active';
  };

  const updateSummary = (summary) => {
    document.getElementById('share-summary-active').textContent = Number(summary.active_count) || 0;
    document.getElementById('share-summary-today').textContent = Number(summary.today_access_count) || 0;
    document.getElementById('share-summary-expiring').textContent = Number(summary.expiring_soon_count) || 0;
  };

  const render = () => {
    const visible = shares.filter(matchesFilter);
    count.textContent = `显示 ${visible.length} 条，共 ${shares.length} 条记录`;
    if (visible.length === 0) {
      list.innerHTML = '<div class="share-empty"><strong>当前没有分享记录</strong><span>创建后的分享会出现在这里。</span></div>';
      list.setAttribute('aria-busy', 'false');
      return;
    }
    list.innerHTML = visible.map((share) => {
      const limit = share.download_limit == null ? '不限' : `${share.download_count} / ${share.download_limit}`;
      const revoke = share.status === 'active'
        ? `<button class="toolbar-link toolbar-link-danger js-share-revoke" type="button" data-share-id="${escapeHtml(share.id)}">撤销</button>`
        : '<span class="muted">-</span>';
      return `
        <article class="share-row" data-share-id="${escapeHtml(share.id)}">
          <div class="share-object-cell"><strong title="${escapeHtml(share.object_key)}">${escapeHtml(share.object_name)}</strong><span>${escapeHtml(share.object_type_label || '文件')} · ${escapeHtml(share.object_key)}</span></div>
          <span class="share-status share-status-${escapeHtml(share.status)}">${escapeHtml(statusLabel(share.status))}</span>
          <div class="share-expiry-cell"><span>${escapeHtml(formatTime(share.expires_at))}</span><small>${share.status === 'active' ? '到期自动失效' : '已停止访问'}</small></div>
          <div class="share-count-cell"><strong>${Number(share.access_count) || 0} 次访问</strong><span>${escapeHtml(limit)} 次下载</span></div>
          <div class="share-protection-cell"><span>${share.password_protected ? '密码保护' : '无需密码'}</span><small>${share.download_limit == null ? '不限次数' : `剩余 ${share.remaining_downloads} 次`}</small></div>
          <div class="share-action-cell">${revoke}</div>
        </article>`;
    }).join('');
    list.setAttribute('aria-busy', 'false');
  };

  const refresh = async () => {
    refreshButton.disabled = true;
    try {
      const payload = await requestJson('/api/shares');
      shares = Array.isArray(payload.shares) ? payload.shares : [];
      updateSummary(payload.summary || {});
      render();
    } catch (error) {
      list.innerHTML = `<div class="share-empty share-empty-error"><strong>分享加载失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      list.setAttribute('aria-busy', 'false');
    } finally {
      refreshButton.disabled = false;
    }
  };

  const setCreatePanelOpen = (open) => {
    createPanel.classList.toggle('hidden', !open);
    openButton.setAttribute('aria-expanded', String(open));
    if (open) document.getElementById('share-object-key').focus();
  };

  openButton.setAttribute('aria-expanded', 'false');
  openButton.addEventListener('click', () => setCreatePanelOpen(createPanel.classList.contains('hidden')));
  closeButton.addEventListener('click', () => setCreatePanelOpen(false));
  refreshButton.addEventListener('click', refresh);

  filterButtons.forEach((button) => button.addEventListener('click', () => {
    activeFilter = button.dataset.shareFilter || 'all';
    filterButtons.forEach((item) => {
      const active = item === button;
      item.classList.toggle('is-active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    render();
  }));

  createForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    createButton.disabled = true;
    createdLink.classList.add('hidden');
    try {
      const rawLimit = document.getElementById('share-download-limit').value.trim();
      const payload = await requestJson('/api/shares', {
        method: 'POST',
        body: JSON.stringify({
          object_key: document.getElementById('share-object-key').value.trim(),
          expires_in_hours: Number(document.getElementById('share-expires-hours').value),
          password: document.getElementById('share-password').value,
          download_limit: rawLimit ? Number(rawLimit) : null,
        }),
      });
      createdUrl.value = payload.share_url;
      createdLink.classList.remove('hidden');
      document.getElementById('share-password').value = '';
      showFeedback(payload.message);
      await refresh();
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      createButton.disabled = false;
    }
  });

  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(createdUrl.value);
      showFeedback('分享链接已复制');
    } catch (_) {
      createdUrl.select();
      document.execCommand('copy');
      showFeedback('分享链接已复制');
    }
  });

  list.addEventListener('click', async (event) => {
    const button = event.target.closest('.js-share-revoke');
    if (!button) return;
    if (!window.confirm('确认撤销这条分享吗？撤销后原链接立即失效。')) return;
    button.disabled = true;
    try {
      const payload = await requestJson(`/api/shares/${encodeURIComponent(button.dataset.shareId)}`, {method: 'DELETE'});
      showFeedback(payload.message);
      await refresh();
    } catch (error) {
      showFeedback(error.message, 'error');
      button.disabled = false;
    }
  });

  refresh();
})();
