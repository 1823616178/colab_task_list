import { test } from 'node:test'
import assert from 'node:assert/strict'
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { readWorkflowConfigFile } from '../server/services/workflowConfigFiles.ts'
import { updateWorkflowInputImage } from '../server/services/workflowImageUpload.ts'

const repoRoot = fileURLToPath(new URL('../../', import.meta.url))
const slug = 'spark_h3_lbh_lightx2v'
const canvasName = 'spark_h3_lbh_official_lightx2v_4step_5p2s_i2va.json'

test('画布 JSON 不作为 Runner 配置，适配配置读取独立 API 图', async () => {
  assert.equal(await readWorkflowConfigFile(path.join(repoRoot, 'workflows', canvasName)), null)
  const config = await readWorkflowConfigFile(path.join(repoRoot, 'workflows', `${slug}.json`))
  assert.ok(config?.api_json)
  assert.equal(config.optional_last_frame, true)
  const api = JSON.parse(await readFile(path.resolve(repoRoot, 'workflows', config.api_json), 'utf8'))
  assert.equal(api['4'].class_type, 'SaveVideo')
  assert.equal(api['4'].inputs.format, 'auto')

  // Every model selected by the graph must be installed under its selected filename.
  const models = new Set(config.models?.map(model => `${model.comfy_dir}/${path.basename(String(model.filename))}`))
  const loaders = { UNETLoader: ['diffusion_models', 'unet_name'], CLIPLoader: ['text_encoders', 'clip_name'], VAELoader: ['vae', 'vae_name'], LoraLoaderModelOnly: ['loras', 'lora_name'], MinimaxH3LatentUpscaler3D: ['latent_upscale_models', 'model_name'] } as const
  for (const node of Object.values(api) as { class_type: string, inputs: Record<string, unknown> }[]) {
    assert.ok(!node.class_type.includes('SparkAttention'))
    const loader = loaders[node.class_type as keyof typeof loaders]
    if (loader) assert.ok(models.has(`${loader[0]}/${node.inputs[loader[1]]}`))
    for (const value of Object.values(node.inputs)) {
      if (Array.isArray(value)) assert.ok(api[value[0]], `缺少上游节点 ${value[0]}`)
    }
  }
})

test('两阶段工作流的首尾帧可独立上传，并被两轮 conditioning 共用', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'spark-h3-images-'))
  try {
    await mkdir(path.join(root, 'workflows'))
    await mkdir(path.join(root, 'flows'))
    const configSource = path.join(repoRoot, 'workflows', `${slug}.json`)
    const config = await readWorkflowConfigFile(configSource)
    assert.ok(config?.api_json)
    const apiSource = path.resolve(repoRoot, 'workflows', config.api_json)
    const sourceText = await readFile(apiSource, 'utf8')
    await copyFile(configSource, path.join(root, 'workflows', `${slug}.json`))
    const apiPath = path.resolve(root, 'workflows', config.api_json)
    await writeFile(apiPath, sourceText)

    assert.deepEqual(await Promise.all([
      updateWorkflowInputImage(root, slug, '9', 'first.png', Buffer.from('first')),
      updateWorkflowInputImage(root, slug, '10', 'last.jpg', Buffer.from('last')),
    ]), ['updated', 'updated'])
    const api = JSON.parse(await readFile(apiPath, 'utf8'))
    for (const id of ['16', '19']) {
      assert.deepEqual(api[id].inputs.first_frame, ['9', 0])
      assert.deepEqual(api[id].inputs.last_frame, ['10', 0])
      assert.deepEqual(api[id].inputs.prompt, ['14', 0])
    }
    assert.equal(await readFile(path.join(root, 'image', api['9'].inputs.image), 'utf8'), 'first')
    assert.equal(await readFile(path.join(root, 'image', api['10'].inputs.image), 'utf8'), 'last')
    assert.equal(await readFile(apiSource, 'utf8'), sourceText)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
