/**
 * node_runner.mjs
 * Persistent Node.js runner for SDEverywhere models.
 *
 * Protocol (newline-delimited JSON over stdin/stdout):
 *   Startup output: { "ready": true, "startTime": N, "endTime": N,
 *                     "outputVarIds": [...], "outputVarNames": [...] }
 *   Input  (stdin): { "inputs": [v0, v1, ...] }
 *   Output (stdout): { "time": [...], "outputs": { "_var_id": [...] } }
 *   Error  (stdout): { "error": "message" }
 */

import { createInterface } from 'readline'
import { pathToFileURL } from 'url'
import { createSynchronousModelRunner } from '@sdeverywhere/runtime'

const modelPath = process.argv[2]
if (!modelPath) {
  process.stderr.write('Usage: node node_runner.mjs <path/to/generated-model.js>\n')
  process.exit(1)
}

// Load the generated model (ES module with async default export)
let runner
try {
  const modelUrl = pathToFileURL(modelPath).href
  const { default: loadGeneratedModel } = await import(modelUrl)
  const genModel = await loadGeneratedModel()
  runner = createSynchronousModelRunner(genModel)
} catch (e) {
  process.stdout.write(JSON.stringify({ error: 'Failed to load model: ' + e.message }) + '\n')
  process.exit(1)
}

// Gather metadata from a sample outputs object
const sampleOutputs = runner.createOutputs()
const outputVarIds = sampleOutputs.varSeries.map(s => s.varId)
const outputVarNames = sampleOutputs.varSeries.map(s => {
  // varId is like "_total_inventory"; convert to readable form if no varName available
  return s.varId
})

// Signal ready to Python
process.stdout.write(JSON.stringify({
  ready: true,
  startTime: sampleOutputs.startTime,
  endTime: sampleOutputs.endTime,
  saveFreq: sampleOutputs.saveFreq,
  outputVarIds
}) + '\n')

// Process commands line by line from stdin
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity })

rl.on('line', line => {
  line = line.trim()
  if (!line) return

  let cmd
  try {
    cmd = JSON.parse(line)
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: 'Invalid JSON: ' + e.message }) + '\n')
    return
  }

  if (!Array.isArray(cmd.inputs)) {
    process.stdout.write(JSON.stringify({ error: 'Expected {"inputs": [...]}' }) + '\n')
    return
  }

  try {
    const outputs = runner.createOutputs()
    runner.runModelSync(cmd.inputs, outputs)

    // Extract time array and per-variable value arrays from varSeries
    const time = outputs.varSeries[0].points.map(p => p.x)
    const outData = {}
    for (const series of outputs.varSeries) {
      outData[series.varId] = series.points.map(p => p.y)
    }

    process.stdout.write(JSON.stringify({ time, outputs: outData }) + '\n')
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: e.message }) + '\n')
  }
})

rl.on('close', () => process.exit(0))
