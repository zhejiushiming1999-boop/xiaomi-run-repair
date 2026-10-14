// Encode screenshot reconstruction with the official Garmin FIT SDK and verify it.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const args=Object.fromEntries(process.argv.slice(2).reduce((pairs,value,i,all)=>{
    if(i%2===0){if(!value.startsWith('--')||all[i+1]===undefined)throw Error('Expected --input, --output, --sdk arguments');
        pairs.push([value.slice(2),all[i+1]]);}return pairs;},[]));
assert(args.input&&args.output,'Required: --input fit-input.json --output output.fit [--sdk SDK/src/index.js]');
const output=path.resolve(args.output);
const validation=path.join(path.dirname(output),path.basename(output,path.extname(output))+'.fit-validation.json');
assert(!fs.existsSync(output)&&!fs.existsSync(validation),'Refusing to overwrite an existing artifact');
const {Encoder,Decoder,Stream,Profile}=args.sdk
    ? await import(pathToFileURL(path.resolve(args.sdk)).href):await import('@garmin/fitsdk');
const payload=JSON.parse(fs.readFileSync(args.input,'utf8'));
const records=payload.records;
assert(Array.isArray(records)&&records.length>=2,'At least two records required');
const first=new Date(records[0].timestamp),end=new Date(records.at(-1).timestamp);
const duration=(end-first)/1000, distance=payload.distance_m;
assert.equal(duration,payload.duration_s,'Timeline and declared duration differ');
assert(Number.isFinite(distance)&&distance>0);
assert.equal(records.length,duration+1,'Expected a complete one-second timeline');
for(let i=0;i<records.length;i++){
    const r=records[i];
    assert.equal((new Date(r.timestamp)-first)/1000,i,'Non-contiguous timeline');
    for(const key of ['heartRate','cadence','stepLength','speed','distance'])assert(Number.isFinite(r[key]),`Missing ${key}`);
    assert(r.cadence>0&&r.cadence<=127&&r.stepLength>0&&r.stepLength<6553.5);
    assert(r.heartRate>=1&&r.heartRate<=255&&r.speed>=0&&r.speed<65.535);
    assert.equal(r.positionLat===undefined,r.positionLong===undefined,'GPS coordinate pair incomplete');
}
assert(Math.abs(records.at(-1).distance-distance)<0.001);
const mean=values=>values.reduce((a,b)=>a+b,0)/values.length;
const fraction=value=>{const q=Math.round(value*128);return {whole:Math.floor(q/128),fraction:(q%128)/128};};
const avgCad=fraction(mean(records.map(r=>r.cadence)));
const maxCad=fraction(Math.max(...records.map(r=>r.cadence)));
const avgStep=mean(records.map(r=>r.stepLength));
const avgHr=Math.round(mean(records.map(r=>r.heartRate)));
const encoder=new Encoder(),nums=Profile.MesgNum;
encoder.onMesg(nums.FILE_ID,{type:'activity',manufacturer:'development',product:1,
    timeCreated:first,productName:'Mi Fitness screenshot approx'});
encoder.onMesg(nums.FILE_CREATOR,{softwareVersion:200});
encoder.onMesg(nums.DEVICE_INFO,{timestamp:first,deviceIndex:0,manufacturer:'development',product:1,
    productName:'Screenshot reconstruction'});
encoder.onMesg(nums.EVENT,{timestamp:first,event:'timer',eventType:'start',eventGroup:0});
for(const r of records){
    const c=fraction(r.cadence);
    const m={timestamp:new Date(r.timestamp),heartRate:r.heartRate,cadence:c.whole,
        fractionalCadence:c.fraction,cadence256:r.cadence,stepLength:r.stepLength,
        speed:r.speed,enhancedSpeed:r.speed,distance:r.distance};
    if(r.positionLat!==undefined){m.positionLat=r.positionLat;m.positionLong=r.positionLong;}
    encoder.onMesg(nums.RECORD,m);
}
encoder.onMesg(nums.EVENT,{timestamp:end,event:'timer',eventType:'stopAll',eventGroup:0});
const summary={timestamp:end,startTime:first,totalElapsedTime:duration,totalTimerTime:duration,
    totalDistance:distance,totalCalories:payload.calories,
    avgSpeed:distance/duration,enhancedAvgSpeed:distance/duration,
    maxSpeed:Math.max(...records.map(r=>r.speed)),enhancedMaxSpeed:Math.max(...records.map(r=>r.speed)),
    avgHeartRate:avgHr,maxHeartRate:Math.max(...records.map(r=>r.heartRate)),
    avgCadence:avgCad.whole,avgFractionalCadence:avgCad.fraction,
    maxCadence:maxCad.whole,maxFractionalCadence:maxCad.fraction,
    avgStepLength:avgStep,sport:'running',subSport:'generic'};
if(payload.steps!==null&&payload.steps!==undefined){
    summary.totalCycles=Math.floor(payload.steps/2);summary.totalFractionalCycles=(payload.steps%2)/2;}
encoder.onMesg(nums.LAP,{...summary,messageIndex:0,event:'lap',eventType:'stop',lapTrigger:'manual'});
encoder.onMesg(nums.SESSION,{...summary,messageIndex:0,event:'session',eventType:'stop',
    firstLapIndex:0,numLaps:1,sportProfileName:'Screenshot approx'});
encoder.onMesg(nums.ACTIVITY,{timestamp:end,totalTimerTime:duration,numSessions:1,type:'manual',event:'activity',eventType:'stop'});
const bytes=encoder.close(),decoder=new Decoder(Stream.fromByteArray(bytes));
assert(decoder.isFIT()&&decoder.checkIntegrity(),'FIT header/length/CRC check failed');
const {messages,errors}=decoder.read();
assert.equal(errors.length,0,JSON.stringify(errors));
const decoded=messages.recordMesgs,session=messages.sessionMesgs[0];
assert.equal(decoded.length,records.length);
assert.equal(messages.sessionMesgs.length,1);assert.equal(messages.lapMesgs.length,1);
assert(Math.abs(session.totalDistance-distance)<=0.011);assert.equal(session.totalTimerTime,duration);
assert(Math.abs(session.avgStepLength-avgStep)<=0.051);
assert(Math.abs(2*(session.avgCadence+session.avgFractionalCadence)-2*mean(records.map(r=>r.cadence)))<=1/128+1e-6);
let gps=0,maxCadError=0,maxStepError=0;
for(let i=0;i<decoded.length;i++){
    const a=records[i],b=decoded[i];
    assert.equal(new Date(b.timestamp).toISOString(),new Date(a.timestamp).toISOString());
    assert.equal(b.heartRate,a.heartRate);
    assert(Number.isFinite(b.stepLength)&&Number.isFinite(b.cadence));
    maxCadError=Math.max(maxCadError,Math.abs(b.cadence+b.fractionalCadence-a.cadence)*2);
    maxStepError=Math.max(maxStepError,Math.abs(b.stepLength-a.stepLength));
    assert(Math.abs(b.distance-a.distance)<=0.011);assert(Math.abs(b.speed-a.speed)<=0.0011);
    if(a.positionLat!==undefined){gps++;assert.equal(b.positionLat,a.positionLat);assert.equal(b.positionLong,a.positionLong);}
    else assert(b.positionLat===undefined&&b.positionLong===undefined);
}
assert(maxCadError<=1/128+1e-6&&maxStepError<=0.051);
const report={file:path.basename(output),integrity:'PASS',decode_errors:errors.length,
    records:decoded.length,native_cadence_records:decoded.length,native_step_length_records:decoded.length,
    gps_records:gps,avg_cadence_spm:2*(session.avgCadence+session.avgFractionalCadence),
    avg_step_length_cm:session.avgStepLength/10,
    step_length_unit:'mm',cadence_unit:'single-foot cycles/minute',
    max_cadence_quantization_error_spm:maxCadError,max_step_length_quantization_error_mm:maxStepError,
    average_pace_check:payload.average_pace_check,source:payload.source,
    curve_variation:{heart_rate:new Set(decoded.map(r=>r.heartRate)).size>1,
        cadence:new Set(decoded.map(r=>r.cadence256)).size>1,
        step_length:new Set(decoded.map(r=>r.stepLength)).size>1,
        speed:new Set(decoded.map(r=>r.speed)).size>1},
    actual_platform_import_verified:false};
fs.mkdirSync(path.dirname(output),{recursive:true});
fs.writeFileSync(output,bytes,{flag:'wx'});fs.writeFileSync(validation,JSON.stringify(report,null,2),{flag:'wx'});
console.log(JSON.stringify(report,null,2));
