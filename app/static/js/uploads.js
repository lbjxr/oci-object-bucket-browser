(() => {
  const list = document.getElementById('tasks-page-list');
  if (!list) return;

  const feedback = document.getElementById('task-feedback');
  const refreshButton = document.getElementById('refresh-tasks');
  const clearButton = document.getElementById('clear-completed-tasks');
  const taskCount = document.getElementById('task-count');
  const activeMetric = document.getElementById('summary-active');
  const todayMetric = document.getElementById('summary-today-bytes');
  const failedMetric = document.getElementById('summary-failed');
  const filterButtons = Array.from(document.querySelectorAll('[data-task-filter]'));
  let tasks = [];
  let activeFilter = 'all';
  let refreshTimer = null;

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));

  const formatBytes = (bytes) => {
    const numeric = Number(bytes) || 0;
    if (numeric <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
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
    try {
      return new Date(value).toLocaleString('zh-CN', {hour12: false});
    } catch (_) {
      return value;
    }
  };

  const jsonRequest = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', ...(options.headers || {})},
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || payload.message || `请求失败 (${response.status})`);
    }
    return payload;
  };

  const showFeedback = (message, type = 'success') => {
    feedback.textContent = message;
    feedback.className = `alert ${type === 'error' ? 'alert-error' : 'alert-success'}`;
  };

  const statusClass = (status) => ({
    queued: 'task-status-neutral',
    running: 'task-status-active',
    finalizing: 'task-status-active',
    completed: 'task-status-success',
    failed: 'task-status-danger',
    canceled: 'task-status-neutral',
  }[status] || 'task-status-neutral');

  const strategyLabel = (strategy) => ({
    'single-put-server-proxy': '单请求入桶',
    'oci-multipart-server-proxy': 'Multipart 并行',
  }[strategy] || strategy || '-');

  const matchesFilter = (task) => {
    if (activeFilter === 'all') return true;
    if (activeFilter === 'active') return ['queued', 'running', 'finalizing'].includes(task.status);
    return task.status === activeFilter;
  };

  const taskActions = (task) => {
    const actions = [];
    if (task.status === 'failed') {
      actions.push(`<button class="toolbar-link js-task-retry" type="button" data-task-id="${escapeHtml(task.task_id)}">重试</button>`);
      actions.push(`<button class="toolbar-link js-task-error-toggle" type="button" data-task-id="${escapeHtml(task.task_id)}" aria-expanded="false">失败原因</button>`);
    }
    if (['queued', 'running', 'finalizing'].includes(task.status)) {
      actions.push(`<button class="toolbar-link toolbar-link-danger js-task-cancel" type="button" data-task-id="${escapeHtml(task.task_id)}">取消</button>`);
    }
    return actions.join('') || '<span class="muted">-</span>';
  };

  const render = () => {
    const visible = tasks.filter(matchesFilter);
    taskCount.textContent = `显示 ${visible.length} 个，共 ${tasks.length} 个任务`;
    if (visible.length === 0) {
      const message = tasks.length === 0 ? '还没有上传任务。' : '当前筛选条件下没有任务。';
      list.innerHTML = `<div class="task-empty"><strong>${message}</strong><span>从文件页发起上传后，后台入桶状态会显示在这里。</span></div>`;
      list.setAttribute('aria-busy', 'false');
      return;
    }
    list.innerHTML = visible.map((task) => {
      const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
      const error = task.last_error || task.error || '未提供失败原因';
      const statusLabel = task.status_label || task.status;
      const phaseLabel = task.retry_label || task.phase_label || task.current_phase || '-';
      return `
        <article class="task-row" data-task-status="${escapeHtml(task.status)}" data-task-id="${escapeHtml(task.task_id)}">
          <div class="task-object-cell">
            <strong title="${escapeHtml(task.object_name)}">${escapeHtml(task.object_name)}</strong>
            <span>${formatBytes(task.total_size)}</span>
          </div>
          <div class="task-progress-cell">
            <div class="task-progress-copy"><span>${progress.toFixed(progress % 1 === 0 ? 0 : 1)}%</span><span>${formatBytes(task.uploaded_bytes)} / ${formatBytes(task.total_size)}</span></div>
            <div class="upload-progress-track"><div class="upload-progress-bar" style="width:${progress}%"></div></div>
          </div>
          <div class="task-state-cell">
            <span class="task-status ${statusClass(task.status)}">${escapeHtml(statusLabel)}</span>
            <small>${escapeHtml(phaseLabel)}</small>
          </div>
          <span class="task-strategy-cell">${escapeHtml(strategyLabel(task.strategy))}<small>并发 ${escapeHtml(task.parallelism || 1)}</small></span>
          <time class="task-time-cell">${escapeHtml(formatTime(task.updated_at))}</time>
          <div class="task-action-cell">${taskActions(task)}</div>
          <div class="task-error-detail hidden" data-task-error="${escapeHtml(task.task_id)}"><strong>最近一次失败</strong><p>${escapeHtml(error)}</p></div>
        </article>`;
    }).join('');
    list.setAttribute('aria-busy', 'false');
  };

  const updateSummary = (summary = {}) => {
    activeMetric.textContent = Number(summary.active_count) || 0;
    failedMetric.textContent = Number(summary.failed_count) || 0;
    todayMetric.textContent = formatBytes(summary.today_uploaded_bytes);
  };

  const scheduleRefresh = () => {
    window.clearTimeout(refreshTimer);
    const hasActiveTasks = tasks.some((task) => ['queued', 'running', 'finalizing'].includes(task.status));
    refreshTimer = window.setTimeout(() => refreshTasks({silent: true}), hasActiveTasks ? 3000 : 12000);
  };

  const refreshTasks = async ({silent = false} = {}) => {
    if (!silent) refreshButton.disabled = true;
    try {
      const payload = await jsonRequest('/api/server-uploads/tasks?limit=100');
      tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      updateSummary(payload.summary);
      render();
    } catch (error) {
      list.innerHTML = `<div class="task-empty task-empty-error"><strong>任务加载失败</strong><span>${escapeHtml(error.message)}</span><button class="button button-secondary button-inline js-task-reload" type="button">重新加载</button></div>`;
      list.setAttribute('aria-busy', 'false');
    } finally {
      refreshButton.disabled = false;
      scheduleRefresh();
    }
  };

  filterButtons.forEach((button) => button.addEventListener('click', () => {
    activeFilter = button.dataset.taskFilter || 'all';
    filterButtons.forEach((item) => {
      const selected = item === button;
      item.classList.toggle('is-active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    render();
  }));

  list.addEventListener('click', async (event) => {
    const retryButton = event.target.closest('.js-task-retry');
    const cancelButton = event.target.closest('.js-task-cancel');
    const errorButton = event.target.closest('.js-task-error-toggle');
    const reloadButton = event.target.closest('.js-task-reload');
    if (reloadButton) {
      await refreshTasks();
      return;
    }
    if (errorButton) {
      const detail = list.querySelector(`[data-task-error="${CSS.escape(errorButton.dataset.taskId)}"]`);
      const willOpen = detail?.classList.contains('hidden');
      detail?.classList.toggle('hidden', !willOpen);
      errorButton.setAttribute('aria-expanded', String(Boolean(willOpen)));
      return;
    }
    const actionButton = retryButton || cancelButton;
    if (!actionButton) return;
    const taskId = actionButton.dataset.taskId;
    actionButton.disabled = true;
    try {
      const action = retryButton ? 'retry' : 'cancel';
      const payload = await jsonRequest(`/api/server-uploads/tasks/${encodeURIComponent(taskId)}/${action}`, {method: 'POST'});
      showFeedback(payload.message || (retryButton ? '任务已重新排队' : '任务已取消'));
      await refreshTasks({silent: true});
    } catch (error) {
      showFeedback(error.message, 'error');
      actionButton.disabled = false;
    }
  });

  refreshButton.addEventListener('click', () => refreshTasks());
  clearButton.addEventListener('click', async () => {
    clearButton.disabled = true;
    try {
      const payload = await jsonRequest('/api/server-uploads/tasks/cleanup-completed', {method: 'POST'});
      showFeedback(payload.message);
      await refreshTasks({silent: true});
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      clearButton.disabled = false;
    }
  });

  todayMetric.textContent = formatBytes(todayMetric.dataset.bytes);
  refreshTasks();
})();
