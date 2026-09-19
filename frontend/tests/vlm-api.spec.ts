import { describe, expect, it } from 'vitest'
import { setTransport } from '../src/api/transport'
import { FakeTransport, okEnvelope } from './helpers'
import { vlmApi } from '../src/api/endpoints/vlm'

// P8 VLM 三端点契约（路径/方法/请求体/幂等键）。
const DRAFT = '/cases/c1/vlm/draft'
const DRAFTS = '/cases/c1/vlm/drafts'
const VERIFY = '/cases/c1/vlm/drafts/pp-1/verify'

function setup(): FakeTransport {
  const t = new FakeTransport([
    { match: (r) => r.path === DRAFT,
      respond: (r) => okEnvelope({ received: r.body }) },
    { match: (r) => r.path === DRAFTS,
      respond: () => okEnvelope({ drafts: [] }) },
    { match: (r) => r.path === VERIFY,
      respond: (r) => okEnvelope({ received: r.body }) },
  ])
  setTransport(t)
  return t
}

describe('vlmApi.draft（POST 发起分析）', () => {
  it('路径/方法/请求体正确，写操作带幂等键', async () => {
    const t = setup()
    await vlmApi.draft('c1', {
      image_uri: 'evidence/m_1/a.jpg',
      content_class: 'invoice',
      instruction: '提取抬头',
      subject_type: 'person',
      subject_id: 'p1',
    })
    const call = t.calls.find((c) => c.path === DRAFT)!
    expect(call.method).toBe('POST')
    expect(call.body).toEqual({
      image_uri: 'evidence/m_1/a.jpg',
      content_class: 'invoice',
      instruction: '提取抬头',
      subject_type: 'person',
      subject_id: 'p1',
    })
    expect(call.headers?.['Idempotency-Key']).toBeTruthy()
  })
})

describe('vlmApi.list（GET 草案队列）', () => {
  it('GET 解包 drafts，不带幂等键', async () => {
    const t = setup()
    const out = await vlmApi.list('c1')
    expect(out).toEqual([])
    const call = t.calls.find((c) => c.path === DRAFTS)!
    expect(call.method).toBe('GET')
    expect(call.headers?.['Idempotency-Key']).toBeUndefined()
  })
})

describe('vlmApi.verify（POST 人验通过）', () => {
  it('路径含 proposal_id，只传核验字段', async () => {
    const t = setup()
    await vlmApi.verify('c1', 'pp-1', {
      verify_conclusion: '与原件一致',
      subject_type: 'person',
      subject_id: 'p1',
      clue_id: 'L1',
    })
    const call = t.calls.find((c) => c.path === VERIFY)!
    expect(call.method).toBe('POST')
    expect(call.body).toEqual({
      verify_conclusion: '与原件一致',
      subject_type: 'person',
      subject_id: 'p1',
      clue_id: 'L1',
    })
    expect(call.headers?.['Idempotency-Key']).toBeTruthy()
  })
})
