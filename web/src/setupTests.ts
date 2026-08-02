import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// vitest.config.ts doesn't set `test.globals: true`, so @testing-library/react
// can't auto-detect a test framework to hook its automatic cleanup into —
// without this, DOM from one test leaks into the next.
afterEach(() => {
  cleanup()
})
