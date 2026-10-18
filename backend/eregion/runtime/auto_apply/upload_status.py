"""Observed upload notices shared by field and page scans; no candidate semantics."""
UPLOAD_STATUS = r"""
  const successfulUploads = root => [...(root?.querySelectorAll('[data-automation-id="file-upload-item"]') || [])]
    .filter(item => item.querySelector('[data-automation-id="file-upload-successful"]'))
    .map(item => (item.querySelector('[data-automation-id="file-upload-item-name"]')?.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const isUploadSuccessNotice = node => {
    if (node.matches('[data-automation-id="errorMessage"], .field-error, .error-message, [aria-invalid="true"]')) return false;
    const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
    const upload = node.closest('[data-automation-id="attachments-FileUpload"]');
    if (!upload) return false;
    const names = successfulUploads(upload);
    // Exact observed Workday status plus a successful attachment card. A filename,
    // role=alert, or a generic word such as success alone is insufficient.
    return names.some(name => text === `${name} successfully uploaded`);
  };
"""
