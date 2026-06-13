/**
 * Index — minimal frontend (counterpart of MainActivity): goal box, Run/Stop,
 * onboarding status, a log pane. Programmatic UI on purpose; a designed surface
 * is Phase 4. Run flow: ensure the accessibility extension is enabled -> load
 * the card + settings -> runLeg.
 *
 * Phase 1 demo path is single-app (runLeg); the package is a field. The NL
 * cross-app flow entry is Phase 3.
 */
import common from '@ohos.app.ability.common';
import { runLeg, LlmConfig } from '../relay/nativeRunner';
import { SettingsStore } from '../settings/SettingsStore';
import { AssetInstaller } from '../device/AssetInstaller';
import { DeviceBridge } from '../device/DeviceBridge';
import { OverlayController } from '../device/OverlayController';
import { setInteraction, OverlayInteraction } from '../relay/interaction';
import { RelayAccessibilityExtension } from '../device/RelayAccessibilityExtension';

@Entry
@Component
struct Index {
  @State goal: string = '';
  @State pkg: string = 'com.aliyun.tongyi';
  @State status: string = '';
  @State log: string = '';

  private ctx(): common.UIAbilityContext {
    return getContext(this) as common.UIAbilityContext;
  }

  aboutToAppear(): void {
    setInteraction(new OverlayInteraction());
    OverlayController.bind((s: string) => this.appendLog(`· ${s}`), (p: string) => {});
    this.refreshStatus();
  }

  async refreshStatus(): Promise<void> {
    const a11y = RelayAccessibilityExtension.instance !== null;
    const cfg = await SettingsStore.load(this.ctx());
    const configured = SettingsStore.isConfigured(cfg);
    this.status = `${a11y ? '✅' : '❌'} 无障碍服务  |  ${configured ? '✅' : '❌'} LLM 网关`;
  }

  appendLog(line: string): void {
    this.log += line + '\n';
  }

  async onRun(): Promise<void> {
    const goal = this.goal.trim();
    if (goal.length === 0) {
      this.appendLog('请先输入任务。');
      return;
    }
    if (RelayAccessibilityExtension.instance === null) {
      this.appendLog('请先在系统设置里开启 RelayAgent 的无障碍服务。');
      return;
    }
    const cfg: LlmConfig = await SettingsStore.load(this.ctx());
    if (!SettingsStore.isConfigured(cfg)) {
      this.appendLog('请先在设置里配置 LLM 网关。');
      return;
    }
    const card = await AssetInstaller.loadCard(this.ctx(), this.pkg);
    if (card === null) {
      this.appendLog(`未找到 ${this.pkg} 的 manifest 卡片。`);
      return;
    }
    DeviceBridge.resetStop();
    OverlayController.show();
    this.appendLog(`▶ ${goal}`);
    try {
      const summary = await runLeg(card, goal, cfg);
      this.appendLog(`结果: ${JSON.stringify(summary)}`);
    } catch (e) {
      this.appendLog(`出错: ${e}`);
    } finally {
      OverlayController.hide();
    }
  }

  build() {
    Column({ space: 12 }) {
      Text(this.status).fontSize(14)
      TextInput({ placeholder: 'app 包名（如 com.aliyun.tongyi）', text: this.pkg })
        .onChange((v: string) => { this.pkg = v; })
      TextArea({ placeholder: '用一句话描述任务', text: this.goal })
        .onChange((v: string) => { this.goal = v; })
        .height(80)
      Row({ space: 12 }) {
        Button('运行').onClick(() => { this.onRun(); })
        Button('停止').onClick(() => { DeviceBridge.requestStop(); this.appendLog('已请求停止。'); })
      }
      Scroll() {
        Text(this.log).fontSize(12).textAlign(TextAlign.Start)
      }.layoutWeight(1)
    }
    .padding(16)
    .width('100%')
    .height('100%')
  }
}
