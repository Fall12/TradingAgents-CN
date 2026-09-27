<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
onMounted(() => {
  router.replace({ name: 'StrategyScreening' })
})
</script>

<template>
  <div />
</template>
