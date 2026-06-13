// Project-level hvigor entry. The app-level plugin wires the module graph;
// custom build logic (e.g. the asset sync) is run out-of-band by
// scripts/sync_harmony_assets.py before `hvigorw assembleHap`.
import { appTasks } from '@ohos/hvigor-ohos-plugin';

export default {
  system: appTasks,
  plugins: [],
};
