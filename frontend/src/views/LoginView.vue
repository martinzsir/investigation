<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NForm, NFormItem, NInput } from 'naive-ui'
import { LOGIN_FAIL_TEXT } from '../api/errors'
import { useAuthStore } from '../stores/auth'

// FE-P-023a 登录页：
// - 错误文案统一（红线八）：账号/密码/双因子失败文案与状态码一致，不泄露账号存在性
// - 未认证前不请求任何案件数据（仅 /auth/login + 免认证 /health 探针）
// - 像素级对齐 Token v1.2 实测值（FE-D-001）
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const operator = ref('')
const password = ref('')
const loading = ref(false)
const errorText = ref('')

async function submit(): Promise<void> {
  if (loading.value || !operator.value || !password.value) return
  loading.value = true
  errorText.value = ''
  try {
    const okResult = await auth.login(operator.value.trim(), password.value)
    if (okResult) {
      const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
      await router.push(redirect)
    } else {
      errorText.value = LOGIN_FAIL_TEXT
    }
  } catch {
    // 网络/服务端异常同样收敛统一文案（不区分失败原因）
    errorText.value = LOGIN_FAIL_TEXT
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <NCard class="login-card" :bordered="true">
      <template #header>
        <div class="brand">
          <div class="title">孙武侦查官</div>
          <div class="subtitle">确定性侦查推演内核 · Web 会话</div>
        </div>
      </template>
      <NForm label-placement="top" @keyup.enter="submit">
        <NFormItem label="账号">
          <NInput
            v-model:value="operator"
            placeholder="operator"
            :input-props="{ autocomplete: 'username' }"
          />
        </NFormItem>
        <NFormItem label="密码">
          <NInput
            v-model:value="password"
            type="password"
            show-password-on="click"
            placeholder="password"
            :input-props="{ autocomplete: 'current-password' }"
          />
        </NFormItem>
        <NAlert v-if="errorText" type="error" class="login-error" :show-icon="true">
          {{ errorText }}
        </NAlert>
        <NButton
          type="primary"
          block
          attr-type="button"
          :loading="loading"
          :disabled="!operator || !password"
          @click="submit"
        >
          登 录
        </NButton>
      </NForm>
    </NCard>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card {
  width: 360px;
  border-color: var(--sun-border-active);
  /* 发光仅给激活态/警示态（FE-D-006）：登录聚焦卡为激活态 */
  box-shadow: 0 0 24px rgba(110, 222, 233, 0.12);
}
.title {
  font-size: 20px;
  font-weight: 700;
  color: var(--sun-text-primary);
}
.subtitle {
  font-size: 12px;
  color: var(--sun-text-secondary);
}
.login-error {
  margin-bottom: 12px;
}
</style>
