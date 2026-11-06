(() => {
  const done = [];
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  async function run() {
    const newTask = document.querySelector('.new-task-button');
    if (newTask) { newTask.click(); await wait(1000); }
    const bars = Array.from(document.querySelectorAll('.bar-button')).map(b => b.textContent.trim());
    const edu = bars.find(b => b.includes('教育场景'));
    if (!edu) { done.push('没找到教育场景按钮'); return done; }
    document.querySelectorAll('.bar-button').forEach(b => { if (b.textContent.includes('教育场景')) b.click(); });
    await wait(1500);
    done.push('场景库面板：' + (document.querySelector('.edu-panel') ? '已打开' : '未打开'));
    done.push('场景卡片：' + document.querySelectorAll('.edu-card').length + ' 个');
    done.push('筛选项：' + document.querySelectorAll('.edu-filters select').length + ' 个');
    done.push('头部按钮：' + Array.from(document.querySelectorAll('.edu-head-actions button')).map(b => b.textContent.trim()).join(' / '));
    const profileBtn = Array.from(document.querySelectorAll('.edu-head-actions button')).find(b => b.textContent.includes('学习者档案'));
    if (profileBtn) { profileBtn.click(); await wait(900); }
    done.push('档案表单字段：' + document.querySelectorAll('.edu-field').length + ' 个');
    const planBtn = Array.from(document.querySelectorAll('.edu-head-actions button')).find(b => b.textContent.includes('复习计划'));
    if (planBtn) { planBtn.click(); await wait(600); }
    done.push('计划控件：' + document.querySelectorAll('.edu-plan-controls input').length + ' 个');
    return done;
  }
  return run();
})();
