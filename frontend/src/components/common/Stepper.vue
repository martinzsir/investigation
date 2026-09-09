<script setup lang="ts">
// FE-C-006 Stepper：接入向导步骤条。三态：完成（青实心勾）/ 当前（高亮）/ 待办（灰）。
withDefaults(
  defineProps<{
    steps: string[]
    /** 当前步骤索引（0 起） */
    current: number
  }>(),
  {},
)
</script>

<template>
  <ol class="stepper">
    <li
      v-for="(label, i) in steps"
      :key="label"
      class="step"
      :class="{ 'step--done': i < current, 'step--current': i === current }"
    >
      <div class="step-head">
        <span class="step-dot">
          <span v-if="i < current" class="step-check">✓</span>
          <span v-else>{{ i + 1 }}</span>
        </span>
        <span v-if="i < steps.length - 1" class="step-bar" />
      </div>
      <span class="step-label">{{ label }}</span>
    </li>
  </ol>
</template>

<style scoped>
.stepper {
  display: flex;
  align-items: flex-start;
  list-style: none;
  margin: 0;
  padding: 8px 4px 4px;
}
.step {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  position: relative;
}
.step-head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}
.step-dot {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-family: var(--sun-font-mono);
  border: 1px solid var(--sun-border);
  background: var(--sun-input-bg, var(--sun-bg-card));
  color: var(--sun-text-tertiary);
  z-index: 1;
  flex-shrink: 0;
}
.step-bar {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 100%;
  height: 1px;
  background: var(--sun-border);
  z-index: 0;
}
.step-label {
  margin-top: 8px;
  font-size: 12px;
  color: var(--sun-text-tertiary);
  text-align: center;
}
.step--done .step-dot {
  border-color: var(--sun-ok-border);
  background: var(--sun-ok-bg);
  color: var(--sun-ok-text);
}
.step--done .step-bar {
  background: var(--sun-ok-border);
}
.step--done .step-label {
  color: var(--sun-text-secondary);
}
.step--current .step-dot {
  border-color: var(--sun-info-border, var(--sun-ok-border));
  background: var(--sun-info-bg, var(--sun-ok-bg));
  color: var(--sun-info-text, var(--sun-ok-text));
  box-shadow: 0 0 0 3px rgba(110, 222, 233, 0.15);
}
.step--current .step-label {
  color: var(--sun-text-primary);
  font-weight: 600;
}
.step-check {
  font-size: 13px;
}
</style>
