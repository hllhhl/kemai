(() => {
  const done = [];
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  async function run() {
    const newTask = document.querySelector('.new-task-button');
    if (newTask) { newTask.click(); await wait(1000); }
    done.push('顶部：' + Array.from(document.querySelectorAll('.bar-button')).map(b => b.textContent.trim()).join(' / '));
    const hist = Array.from(document.querySelectorAll('.bar-button')).find(b => b.textContent.includes('对话历史'));
    if (hist) { hist.click(); await wait(1200); }
    done.push('搜索框：' + (document.querySelector('.session-search') ? '有' : '无'));
    done.push('置顶/改名：' + document.querySelectorAll('.session-act').length + ' 个');
    done.push('历史会话：' + Array.from(document.querySelectorAll('.session-title')).map(t => t.textContent.trim()).join(' / '));
    const chip = document.querySelector('.model-chip');
    if (chip) { chip.click(); await wait(1000); }
    const menu = document.querySelector('.model-menu');
    done.push('模型菜单：' + (menu ? '展开，' + menu.querySelectorAll('.model-row').length + ' 个模型' : '未展开'));
    return done;
  }
  return run();
})();
