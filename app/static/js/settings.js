(() => {
  const form = document.getElementById('settings-form');
  if (!form) return;

  const feedback = document.getElementById('settings-feedback');
  const saveButton = document.getElementById('save-settings');
  const validateButton = document.getElementById('validate-settings');
  const cleanupButton = document.getElementById('cleanup-temp-files');
  const fields = {
    readOnly: document.getElementById('read-only-mode'),
    webdavEnabled: document.getElementById('webdav-enabled'),
    webdavUsername: document.getElementById('webdav-username'),
    webdavPassword: document.getElementById('webdav-password'),
    namespace: document.getElementById('storage-namespace'),
    bucket: document.getElementById('storage-bucket'),
    region: document.getElementById('storage-region'),
    prefixRoot: document.getElementById('storage-prefix-root'),
    chunkSize: document.getElementById('upload-chunk-size'),
    parallelism: document.getElementById('upload-parallelism'),
    threshold: document.getElementById('upload-threshold'),
    trashEnabled: document.getElementById('trash-enabled'),
    batchConfirmation: document.getElementById('batch-confirmation-required'),
  };

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

  const sourceLabel = (elementId, overridden) => {
    const element = document.getElementById(elementId);
    if (!element) return;
    element.textContent = overridden ? '环境变量生效' : '本地设置';
    element.className = overridden ? 'setting-source is-environment' : 'setting-source';
  };

  const populate = (settings) => {
    const stored = settings;
    const effective = settings.effective || {};
    const overrides = settings.environment_overrides || {};
    fields.readOnly.checked = Boolean(stored.access?.read_only_mode);
    fields.webdavEnabled.checked = Boolean(stored.webdav?.enabled);
    fields.webdavUsername.value = stored.webdav?.username || '';
    fields.webdavPassword.value = '';
    document.getElementById('webdav-password-state').textContent = stored.webdav?.password_configured ? '密码已安全保存，留空保持不变' : '尚未设置';
    fields.namespace.value = effective.namespace || stored.storage?.namespace || '';
    fields.bucket.value = effective.bucket_name || stored.storage?.bucket_name || '';
    fields.region.value = effective.region || stored.storage?.region || '';
    fields.prefixRoot.value = effective.prefix_root || stored.storage?.prefix_root || '';
    fields.chunkSize.value = effective.chunk_size_mb || stored.upload?.chunk_size_mb || 16;
    fields.parallelism.value = effective.parallelism || stored.upload?.parallelism || 6;
    fields.threshold.value = effective.single_put_threshold_mb || stored.upload?.single_put_threshold_mb || 32;
    fields.trashEnabled.checked = Boolean(stored.safety?.trash_enabled);
    fields.batchConfirmation.checked = stored.safety?.batch_delete_confirmation_required !== false;
    sourceLabel('source-namespace', overrides.OCI_NAMESPACE);
    sourceLabel('source-bucket', overrides.OCI_BUCKET_NAME);
    sourceLabel('source-region', overrides.OCI_REGION);
    sourceLabel('source-prefix-root', overrides.OCI_PREFIX_ROOT);
    sourceLabel('source-chunk-size', overrides.APP_UPLOAD_CHUNK_SIZE_MB);
    sourceLabel('source-parallelism', overrides.APP_UPLOAD_PARALLELISM);
    sourceLabel('source-threshold', overrides.APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB);
    form.setAttribute('aria-busy', 'false');
  };

  const collect = () => ({
    access: {read_only_mode: fields.readOnly.checked},
    storage: {
      namespace: fields.namespace.value.trim(),
      bucket_name: fields.bucket.value.trim(),
      region: fields.region.value.trim(),
      prefix_root: fields.prefixRoot.value.trim(),
    },
    upload: {
      chunk_size_mb: Number(fields.chunkSize.value),
      parallelism: Number(fields.parallelism.value),
      single_put_threshold_mb: Number(fields.threshold.value),
    },
    safety: {
      trash_enabled: fields.trashEnabled.checked,
      batch_delete_confirmation_required: fields.batchConfirmation.checked,
    },
    webdav: {
      enabled: fields.webdavEnabled.checked,
      username: fields.webdavUsername.value.trim(),
      password: fields.webdavPassword.value,
    },
  });

  const load = async () => {
    try {
      const payload = await requestJson('/api/settings');
      populate(payload.settings);
    } catch (error) {
      form.setAttribute('aria-busy', 'false');
      showFeedback(`设置加载失败：${error.message}`, 'error');
    }
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    saveButton.disabled = true;
    try {
      const payload = await requestJson('/api/settings', {method: 'POST', body: JSON.stringify(collect())});
      populate(payload.settings);
      showFeedback(payload.message);
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      saveButton.disabled = false;
    }
  });

  validateButton.addEventListener('click', async () => {
    validateButton.disabled = true;
    try {
      const payload = await requestJson('/api/settings/validate', {method: 'POST', body: JSON.stringify(collect())});
      showFeedback(payload.message);
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      validateButton.disabled = false;
    }
  });

  cleanupButton.addEventListener('click', async () => {
    cleanupButton.disabled = true;
    try {
      const payload = await requestJson('/api/server-uploads/cleanup', {method: 'POST'});
      showFeedback(payload.message);
    } catch (error) {
      showFeedback(error.message, 'error');
    } finally {
      cleanupButton.disabled = false;
    }
  });

  load();
})();
