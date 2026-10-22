import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { updateWorkflowInputImage } from '../server/services/workflowImageUpload.ts'

test('上传图片只更新声明的 LoadImage 节点，并保留并发更新', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'workflow-images-'))
  try {
    await mkdir(path.join(root, 'workflows'))
    await writeFile(path.join(root, 'workflows', 'example.json'), JSON.stringify({
      api_json: '../api.json',
      input_fields: [{ class_type: 'LoadImage', field: 'image', local_dir: '../image' }],
    }))
    await writeFile(path.join(root, 'api.json'), JSON.stringify({
      '1': { class_type: 'LoadImage', inputs: { image: 'old.png' } },
      '2': { class_type: 'LoadImage', inputs: { image: 'old2.png' } },
      '3': { class_type: 'Other', inputs: { image: 'leave.png' } },
    }))

    assert.equal(await updateWorkflowInputImage(root, 'example', '3', 'bad.png', Buffer.from('x')), 'not_found')
    assert.equal(await updateWorkflowInputImage(root, 'example', '1', 'bad.svg', Buffer.from('x')), 'unsupported')
    const results = await Promise.all([
      updateWorkflowInputImage(root, 'example', '1', 'first.png', Buffer.from('first')),
      updateWorkflowInputImage(root, 'example', '2', 'second.jpg', Buffer.from('second')),
    ])
    assert.deepEqual(results, ['updated', 'updated'])

    const api = JSON.parse(await readFile(path.join(root, 'api.json'), 'utf8'))
    const firstName = api['1'].inputs.image as string
    const secondName = api['2'].inputs.image as string
    assert.match(firstName, /^[\w-]+\.png$/)
    assert.match(secondName, /^[\w-]+\.jpg$/)
    assert.equal(api['3'].inputs.image, 'leave.png')
    assert.equal(await readFile(path.join(root, 'image', firstName), 'utf8'), 'first')
    assert.equal(await readFile(path.join(root, 'image', secondName), 'utf8'), 'second')
    assert.equal((await readdir(path.join(root, 'image'))).length, 2)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
