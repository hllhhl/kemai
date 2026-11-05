(() => {
  const done = [];
  // 关掉「新建复习会话」弹窗（如果刚才误触打开了）
  const cancel = Array.from(document.querySelectorAll('button')).find((button) => (button.textContent || '').trim() === '取消');
  if (cancel) { cancel.click(); done.push('已关闭弹窗'); }

  const select = document.querySelector('select[aria-label="选择复习会话"]');
  if (select) {
    done.push('下拉选项：' + Array.from(select.options).map((option) => `${option.value === '' ? '(空)' : option.value.slice(0, 8)}=${option.textContent.trim().slice(0, 16)}`).join(' / '));
    done.push('当前值：' + (select.value || '(空)'));
  } else {
    done.push('没有复习会话下拉');
  }
  return done;
})();
