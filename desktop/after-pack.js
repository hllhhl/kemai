/**
 * 把 model-runtime 的 node_modules 补进安装目录。
 *
 * electron-builder 的 extraResources 会跳过任何叫 node_modules 的目录（它默认
 * 认为第三方依赖由 dependencies 字段自动处理）。但 model-runtime/bridge.mjs 是
 * 用运行时动态 import 加载 @earendil-works/pi-ai 的，既没法被 esbuild 静态打成一个
 * 文件，也不能靠 NODE_PATH（ESM 不认）。所以这里在 app 目录生成完之后直接拷过去，
 * 位置和开发目录保持一致：resources/model-runtime/node_modules。
 */

const fs = require('node:fs');
const path = require('node:path');

exports.default = async function afterPack(context) {
  const source = path.resolve(__dirname, '..', 'model-runtime', 'node_modules');
  const target = path.join(context.appOutDir, 'resources', 'model-runtime', 'node_modules');

  if (!fs.existsSync(source)) {
    throw new Error(`找不到 model-runtime 依赖：${source}`);
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.cpSync(source, target, { recursive: true, dereference: true });
  console.log(`  • 已补入 model-runtime 依赖 → ${target}`);
};
