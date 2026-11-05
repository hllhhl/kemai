(() => {
  const done = [];
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function run() {
    // 侧栏的「新建任务」不在 nav.main-nav 里，得单独点
    const newTask = document.querySelector('.new-task-button');
    if (newTask) {
      newTask.click();
      done.push('点开新建任务');
      await wait(900);
    } else {
      done.push('没找到新建任务按钮');
      return done;
    }

    const plus = document.querySelector('.composer-plus');
    if (!plus) {
      done.push('没找到＋按钮');
      return done;
    }
    plus.click();
    await wait(700);
    const menu = document.querySelector('.composer-menu');
    done.push('菜单已展开：' + (menu ? '是' : '否'));
    if (menu) {
      done.push('菜单高 ' + Math.round(menu.getBoundingClientRect().height) + 'px');
      // 菜单里到底有什么
      const groups = Array.from(menu.querySelectorAll('.composer-menu-title')).map((t) => t.textContent.trim());
      done.push('分组：' + groups.join(' / '));
      const empty = Array.from(menu.querySelectorAll('.composer-menu-empty')).map((t) => t.textContent.trim());
      if (empty.length) done.push('空态：' + empty.join(' | '));
    }

    // 关键一幕：点输入框（菜单外面），菜单该收起
    const box = document.querySelector('.task-composer textarea');
    if (box) {
      box.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      box.click();
      await wait(500);
      done.push('点输入框后菜单还在：' + (document.querySelector('.composer-menu') ? '是（没关！）' : '否（已关闭）'));
    }
    return done;
  }

  return run();
})();
