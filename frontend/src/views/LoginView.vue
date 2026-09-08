<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NCheckbox, NIcon, NInput, useMessage } from 'naive-ui'
import {
  DocumentTextOutline,
  EyeOffOutline,
  LockClosedOutline,
  PersonOutline,
  ShieldCheckmarkOutline,
  TimeOutline,
  WarningOutline,
} from '@vicons/ionicons5'
import { LOGIN_FAIL_TEXT } from '../api/errors'
import { useAuthStore } from '../stores/auth'

// FE-P-023a 登录页（对齐 23a 登录页.jpeg）：
// - 左品牌区：徽标 + 涉密公告卡 + 部署信息；右登录卡：三要素输入 + 渐变登录按钮
// - 错误文案统一（红线八）：账号/密码/双因子失败文案一致，不泄露账号存在性
// - 未认证前不请求任何案件数据（仅 /auth/login + 免认证 /health 探针）
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const message = useMessage()

const operator = ref('')
const password = ref('')
/** 双因子验证码：23a 第三要素；MVP-0 后端无 2FA 端点，字段保留不阻塞提交 */
const otp = ref('')
/** 记住本机：23a 默认勾选；红线约束 token 仅 sessionStorage，MVP-0 不接持久化 */
const remember = ref(true)
const loading = ref(false)
const errorVisible = ref(false)

async function submit(): Promise<void> {
  if (loading.value || !operator.value || !password.value) return
  loading.value = true
  errorVisible.value = false
  try {
    const okResult = await auth.login(operator.value.trim(), password.value)
    if (okResult) {
      const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
      await router.push(redirect)
    } else {
      errorVisible.value = true
    }
  } catch {
    // 网络/服务端异常同样收敛统一文案（不区分失败原因）
    errorVisible.value = true
  } finally {
    loading.value = false
  }
}

function forgotPassword(): void {
  message.info('内网环境：账号解锁或密码重置请联系系统管理员')
}
</script>

<template>
  <div class="login-page">
    <!-- 背景科技纹理（纯 CSS，内网零外链） -->
    <div class="bg-grid" />
    <div class="bg-glow bg-glow--left" />
    <div class="bg-glow bg-glow--right" />

    <div class="login-wrap">
     <div class="cols">
      <!-- ============ 左：品牌区 ============ -->
      <aside class="brand-panel">
        <div class="brand-top">
          <span class="brand-dot" />
          <span class="brand-name">孙武侦查官</span>
        </div>

        <div class="emblem" aria-hidden="true">
          <svg viewBox="0 0 200 224" fill="none" xmlns="http://www.w3.org/2000/svg">
            <!-- 盾牌外轮廓 -->
            <path
              d="M100 14 L170 40 V110 C170 158 138 190 100 206 C62 190 30 158 30 110 V40 Z"
              stroke="#6EDEE9"
              stroke-width="4"
              stroke-linejoin="round"
            />
            <!-- 齿轮 -->
            <g transform="translate(100 94)" stroke="#6EDEE9" stroke-width="3">
              <circle r="28" />
              <circle r="11" />
              <g fill="#6EDEE9" stroke="none">
                <rect x="-5" y="-39" width="10" height="14" rx="2" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(45)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(90)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(135)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(180)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(225)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(270)" />
                <rect x="-5" y="-39" width="10" height="14" rx="2" transform="rotate(315)" />
              </g>
              <!-- 五角星 -->
              <path
                d="M0 -9.5 L2.8 -3 L9.2 -2.8 L4.2 1.6 L6 8.6 L0 4.6 L-6 8.6 L-4.2 1.6 L-9.2 -2.8 L-2.8 -3 Z"
                fill="#6EDEE9"
                stroke="none"
              />
            </g>
            <!-- 麦穗枝 -->
            <path
              d="M86 170 C68 160 54 140 50 116"
              stroke="#6EDEE9"
              stroke-width="2.6"
              stroke-linecap="round"
            />
            <path
              d="M114 170 C132 160 146 140 150 116"
              stroke="#6EDEE9"
              stroke-width="2.6"
              stroke-linecap="round"
            />
            <g fill="#6EDEE9" opacity="0.9">
              <ellipse cx="62" cy="154" rx="8" ry="3.4" transform="rotate(-38 62 154)" />
              <ellipse cx="54" cy="140" rx="8" ry="3.4" transform="rotate(-52 54 140)" />
              <ellipse cx="51" cy="125" rx="8" ry="3.4" transform="rotate(-68 51 125)" />
              <ellipse cx="54" cy="112" rx="7" ry="3" transform="rotate(-82 54 112)" />
              <ellipse cx="138" cy="154" rx="8" ry="3.4" transform="rotate(38 138 154)" />
              <ellipse cx="146" cy="140" rx="8" ry="3.4" transform="rotate(52 146 140)" />
              <ellipse cx="149" cy="125" rx="8" ry="3.4" transform="rotate(68 149 125)" />
              <ellipse cx="146" cy="112" rx="7" ry="3" transform="rotate(82 146 112)" />
            </g>
          </svg>
        </div>

        <div class="notice-card">
          <div class="notice-title">XX<span class="star">★</span>内部</div>
          <ul class="notice-list">
            <li>
              <NIcon :component="DocumentTextOutline" class="notice-icon" />
              <span>本系统处理XXXX数据</span>
            </li>
            <li>
              <NIcon :component="EyeOffOutline" class="notice-icon notice-icon--amber" />
              <span>严禁截屏外传</span>
            </li>
            <li>
              <NIcon :component="TimeOutline" class="notice-icon notice-icon--amber" />
              <span>操作全程留痕可追溯</span>
            </li>
          </ul>
        </div>
      </aside>

      <div class="v-divider" aria-hidden="true" />

      <!-- ============ 右：登录卡 ============ -->
      <section class="form-panel">
        <div class="login-card">
          <div class="card-topline" aria-hidden="true" />
          <h1 class="card-title">账号登录</h1>

          <div class="field">
            <NInput
              v-model:value="operator"
              size="large"
              placeholder="工号"
              :input-props="{ autocomplete: 'username' }"
              @keyup.enter="submit"
            >
              <template #prefix>
                <NIcon :component="PersonOutline" class="field-icon" />
              </template>
            </NInput>
          </div>

          <div class="field">
            <NInput
              v-model:value="password"
              type="password"
              show-password-on="click"
              size="large"
              placeholder="密码"
              :input-props="{ autocomplete: 'current-password' }"
              @keyup.enter="submit"
            >
              <template #prefix>
                <NIcon :component="LockClosedOutline" class="field-icon" />
              </template>
            </NInput>
          </div>

          <div class="field">
            <NInput
              v-model:value="otp"
              size="large"
              placeholder="双因子验证码"
              :input-props="{ autocomplete: 'one-time-code' }"
              @keyup.enter="submit"
            >
              <template #prefix>
                <NIcon :component="ShieldCheckmarkOutline" class="field-icon field-icon--accent" />
              </template>
            </NInput>
          </div>

          <NCheckbox v-model:checked="remember" class="remember">记住本机</NCheckbox>

          <button
            type="button"
            class="submit"
            :disabled="!operator || !password || loading"
            @click="submit"
          >
            {{ loading ? '登录中…' : '登 录' }}
          </button>

          <button type="button" class="forgot" @click="forgotPassword">忘记密码</button>

          <!-- 红线八：统一错误框——标题不区分失败原因，副文案明示不区分用户是否存在 -->
          <div v-if="errorVisible" class="error-box" role="alert">
            <NIcon :component="WarningOutline" class="error-icon" />
            <div class="error-text">
              <div class="error-title">{{ LOGIN_FAIL_TEXT }}</div>
              <div class="error-sub">统一错误指示不区分用户是否存在</div>
            </div>
          </div>
        </div>
      </section>
     </div>

      <!-- 部署信息：在两栏对齐线之下，仅占左栏宽度（对齐设计稿） -->
      <div class="brand-foot">
        <div class="brand-footer">
          <div class="foot-item">
            <NIcon :component="PersonOutline" class="foot-icon" />
            <div>
              部署单位
              <div class="foot-sub">v0.1.0</div>
            </div>
          </div>
          <div class="foot-sep" />
          <div class="foot-item">
            <div>
              系统单位
              <div class="foot-sub mono">GAS-PT-2026-0418</div>
            </div>
          </div>
          <div class="foot-sep" />
          <div class="foot-item">
            <NIcon :component="ShieldCheckmarkOutline" class="foot-icon" />
            <div>XXX环境</div>
          </div>
        </div>
        <div class="foot-note">仅供内部使用</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  background: var(--sun-bg-base);
}

/* ---- 背景纹理 ---- */
.bg-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(110, 222, 233, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(110, 222, 233, 0.045) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(ellipse 90% 80% at 50% 45%, black 30%, transparent 100%);
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}
.bg-glow--left {
  width: 520px;
  height: 520px;
  left: -120px;
  top: 15%;
  background: rgba(0, 210, 220, 0.08);
}
.bg-glow--right {
  width: 600px;
  height: 600px;
  right: -160px;
  bottom: -120px;
  background: rgba(30, 120, 220, 0.09);
}

.login-wrap {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  max-width: 1240px;
  margin: 0 auto;
  padding: 40px 56px 28px;
  min-height: 100vh;
  box-sizing: border-box;
}

/* 两栏等高：公告卡底边与登录卡底边共线（23a 对齐基准） */
.cols {
  flex: 1 0 auto;
  display: flex;
  align-items: stretch;
  gap: 56px;
}

/* ---- 左：品牌区 ---- */
.brand-panel {
  flex: 0 0 380px;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.brand-top {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, #9df4fb, #00d4e0 60%, #0099aa);
  box-shadow: 0 0 14px rgba(0, 229, 211, 0.9), 0 0 32px rgba(0, 229, 211, 0.4);
}
.brand-name {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 2px;
  color: var(--sun-text-primary);
}
.emblem {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 0;
}
.emblem svg {
  width: 280px;
  filter: drop-shadow(0 0 10px rgba(0, 229, 211, 0.55)) drop-shadow(0 0 32px rgba(0, 229, 211, 0.25));
}

.notice-card {
  border: 1px solid rgba(110, 222, 233, 0.35);
  border-radius: 10px;
  background: rgba(5, 21, 34, 0.66);
  padding: 20px 24px;
  box-shadow: var(--sun-glow-cyan-soft);
}
.notice-title {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--sun-gold);
}
.notice-title .star {
  margin: 0 4px;
  font-size: 15px;
}
.notice-list {
  margin: 14px 0 0;
  padding: 0;
  list-style: none;
}
.notice-list li {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 14px;
  font-size: 15px;
  color: var(--sun-text-secondary);
}
.notice-icon {
  font-size: 22px;
  color: var(--sun-border-active);
  flex: 0 0 auto;
}
.notice-icon--amber {
  color: var(--sun-gold);
}

/* 部署信息块：对齐线之下，宽度同左栏 */
.brand-foot {
  flex: 0 0 auto;
  width: 380px;
  margin-top: 20px;
}
.brand-footer {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-top: 18px;
  border-top: 1px solid rgba(110, 222, 233, 0.18);
  color: var(--sun-text-secondary);
  font-size: 13px;
}
.foot-item {
  display: flex;
  align-items: center;
  gap: 8px;
}
.foot-icon {
  font-size: 26px;
  color: var(--sun-border-active);
}
.foot-sub {
  color: var(--sun-text-tertiary);
  font-size: 12px;
  margin-top: 2px;
}
.foot-sep {
  width: 1px;
  height: 28px;
  background: rgba(110, 222, 233, 0.2);
}
.foot-note {
  margin-top: 14px;
  font-size: 12px;
  color: var(--sun-text-tertiary);
}

/* ---- 中分隔线 ---- */
.v-divider {
  width: 1px;
  align-self: stretch;
  margin: auto 0;
  background: linear-gradient(
    180deg,
    transparent 0%,
    rgba(110, 222, 233, 0.32) 18%,
    rgba(110, 222, 233, 0.32) 82%,
    transparent 100%
  );
}

/* ---- 右：登录卡（拉伸至与左栏等高，底边与公告卡共线） ---- */
.form-panel {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.login-card {
  position: relative;
  flex: 1 1 auto;
  width: 100%;
  margin: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid rgba(110, 222, 233, 0.4);
  border-radius: 12px;
  background: rgba(5, 21, 34, 0.88);
  padding: 44px 44px 36px;
  box-shadow: var(--sun-glow-card);
  overflow: hidden;
}
.card-topline {
  position: absolute;
  top: 0;
  left: 28px;
  right: 28px;
  height: 2px;
  background: var(--sun-gradient-card-line);
}
.card-title {
  margin: 4px 0 28px;
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 2px;
  color: var(--sun-text-primary);
}

.field {
  margin-bottom: 18px;
}
.field-icon {
  font-size: 20px;
  color: var(--sun-text-tertiary);
}
.field-icon--accent {
  color: var(--sun-border-active);
}
.field :deep(.n-input) {
  height: 52px;
  border-radius: 8px;
}
.field :deep(.n-input .n-input__input-el) {
  height: 52px;
  font-size: 15px;
}
.field :deep(.n-input .n-input__prefix) {
  margin-right: 10px;
}
.field :deep(.n-input.n-input--focus-type),
.field :deep(.n-input:focus-within) {
  box-shadow: 0 0 0 3px rgba(110, 222, 233, 0.12);
}

.remember {
  margin: 2px 0 22px;
}
.remember :deep(.n-checkbox-box) {
  border-radius: 4px;
}
.remember :deep(.n-checkbox-box--checked) {
  background: var(--sun-border-active);
  border-color: var(--sun-border-active);
}
.remember :deep(.n-checkbox-box--checked .n-icon) {
  color: #04101c;
}
.remember :deep(.n-checkbox .n-checkbox-label) {
  font-size: 14px;
  color: var(--sun-text-primary);
}

.submit {
  width: 100%;
  height: 56px;
  border: none;
  border-radius: 8px;
  background: var(--sun-gradient-primary);
  color: #ffffff;
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 6px;
  cursor: pointer;
  box-shadow: 0 0 22px rgba(0, 180, 230, 0.35);
  transition: filter 0.15s ease, box-shadow 0.15s ease, opacity 0.15s ease;
}
.submit:hover:not(:disabled) {
  background: var(--sun-gradient-primary-hover);
  box-shadow: 0 0 28px rgba(0, 200, 240, 0.5);
}
.submit:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.forgot {
  display: block;
  margin: 16px auto 0;
  padding: 4px 8px;
  border: none;
  background: none;
  cursor: pointer;
  font-size: 14px;
  color: var(--sun-border-active);
}
.forgot:hover {
  color: var(--sun-primary-hover, #8ce7f2);
  text-decoration: underline;
}

.error-box {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-top: auto;
  padding: 12px 16px;
  border: 1px solid var(--sun-error-border);
  border-radius: 8px;
  background: var(--sun-error-bg);
}
.error-icon {
  font-size: 26px;
  color: var(--sun-error-text);
  flex: 0 0 auto;
  margin-top: 2px;
}
.error-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--sun-error-text);
}
.error-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--sun-text-tertiary);
}

/* ---- 窄屏：仅保留登录卡 ---- */
@media (max-width: 960px) {
  .login-wrap {
    padding: 20px;
  }
  .brand-panel,
  .brand-foot,
  .v-divider {
    display: none;
  }
}
</style>
