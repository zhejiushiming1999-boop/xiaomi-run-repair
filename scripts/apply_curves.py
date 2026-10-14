#!/usr/bin/env python3
"""Apply screenshot-derived curve anchors to a generated base TCX; retain GPS/time."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as E
from repair_tcx import TCX, AX, tag, add, date, number, interpolate, average_pace_report, write_tcx

METRICS={'heart_rate_bpm':(1,255),'cadence_spm':(1,254),
         'pace_s_km':(60,86400),'step_length_cm':(1,500)}
NS={'t':TCX,'a':AX}


def quantified(values, factor=1):
    result=[]; accumulator=0; previous=0
    for value in values:
        accumulator+=value/factor
        current=math.floor(accumulator+1e-7)
        result.append(current-previous); previous=current
    return result


def set_text(node,path,value):
    child=node.find(path,NS)
    if child is None:
        raise ValueError(f'Generated base TCX lacks {path}; run repair_tcx.py first.')
    child.text=str(value)


def apply(source, curves, output, summary=None, fit_input=None):
    source,curves,output=map(Path,(source,curves,output)); summary=summary or {}
    targets=[output,output.with_suffix('.csv'),output.with_suffix('.json')]
    if fit_input is not None: targets.append(Path(fit_input))
    resolved=[p.resolve() for p in targets]
    if len(set(resolved))!=len(resolved) or any(p.exists() for p in targets):
        raise ValueError('Refusing duplicate paths or an existing output file.')
    if source.resolve() in resolved or curves.resolve() in resolved:
        raise ValueError('Refusing to overwrite source files.')
    data=source.read_bytes()
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD/entity declarations are unsupported.')
    root=E.fromstring(data)
    activities=root.findall('t:Activities/t:Activity',NS)
    if len(activities)!=1: raise ValueError('Expected one generated activity.')
    activity=activities[0]; laps=activity.findall('t:Lap',NS)
    if len(laps)!=1 or len(laps[0].findall('t:Track',NS))!=1:
        raise ValueError('Expected one generated lap/track; do not bridge pauses.')
    notes=activity.find('t:Notes',NS)
    if notes is None or not (notes.text or '').startswith('Mi Fitness repair:'):
        raise ValueError('Only process the generated base TCX, never overwrite real sensor samples.')
    lap=laps[0]
    duration=number(lap.findtext('t:TotalTimeSeconds',namespaces=NS),'duration',1,86400)
    distance=number(lap.findtext('t:DistanceMeters',namespaces=NS),'distance',0.01,1000000)
    if duration!=int(duration): raise ValueError('Subsecond timing requires manual analysis.')
    points=lap.findall('t:Track/t:Trackpoint',NS)
    start=date(activity.findtext('t:Id',namespaces=NS))
    seconds=[(date(p.findtext('t:Time',namespaces=NS))-start).total_seconds() for p in points]
    if seconds!=list(range(int(duration)+1)):
        raise ValueError('Base TCX must cover the complete one-second timeline.')
    with curves.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not set(['elapsed_s',*METRICS]).issubset(reader.fieldnames or []):
            raise ValueError('Curve CSV needs elapsed_s, heart_rate_bpm, cadence_spm, pace_s_km, step_length_cm.')
        anchors=list(reader)
    if len(anchors)<2: raise ValueError('At least two anchors required.')
    xs=[number(r['elapsed_s'],'curve time',0,duration) for r in anchors]
    if xs[0]!=0 or xs[-1]!=duration or any(b<=a for a,b in zip(xs,xs[1:])):
        raise ValueError('Anchors must increase uniquely from 0 to the complete activity duration.')
    series={}
    for key,(low,high) in METRICS.items():
        values=[number(r[key],key,low,high) for r in anchors]
        series[key]=[interpolate(t,xs,values) for t in seconds]
    hr=[math.floor(v+0.5) for v in series['heart_rate_bpm']]
    cadence=series['cadence_spm']; step=series['step_length_cm']
    speed=[1000/v for v in series['pace_s_km']]
    integral=sum((a+b)/2 for a,b in zip(speed,speed[1:]))
    scale=distance/integral
    if not 0.5<=scale<=2: raise ValueError('Screenshot pace integral differs greatly from original distance.')
    speed=[v*scale for v in speed]
    cumulative=[0.0]
    for a,b in zip(speed,speed[1:]): cumulative.append(cumulative[-1]+(a+b)/2)
    cumulative[-1]=distance
    native_cad=quantified(cadence); run_cad=quantified(cadence,2)
    if any(v>254 or v<0 for v in native_cad+run_cad): raise ValueError('Encoded cadence outside TCX range.')
    rows=[]; records=[]
    for i,p in enumerate(points):
        set_text(p,'t:DistanceMeters',f'{cumulative[i]:.6f}')
        set_text(p,'t:HeartRateBpm/t:Value',hr[i])
        set_text(p,'t:Cadence',native_cad[i])
        set_text(p,'t:Extensions/a:TPX/a:Speed',f'{speed[i]:.8f}')
        set_text(p,'t:Extensions/a:TPX/a:RunCadence',run_cad[i])
        pos=p.find('t:Position',NS)
        latitude=pos.findtext('t:LatitudeDegrees',namespaces=NS) if pos is not None else ''
        longitude=pos.findtext('t:LongitudeDegrees',namespaces=NS) if pos is not None else ''
        rows.append({'time_utc':p.findtext('t:Time',namespaces=NS),'elapsed_s':int(seconds[i]),
            'heart_rate_bpm':hr[i],'cadence_spm':cadence[i],'pace_s_km':1000/speed[i],
            'speed_m_s':speed[i],'distance_m':cumulative[i],'step_length_cm':step[i],
            'step_length_from_speed_cadence_cm':speed[i]*6000/cadence[i],
            'latitude':latitude,'longitude':longitude,'original_gps':int(pos is not None),
            'source':'screenshot_reconstruction_approximation'})
        record={'timestamp':rows[-1]['time_utc'],'heartRate':hr[i],'cadence':cadence[i]/2,
                'stepLength':step[i]*10,'speed':speed[i],'distance':cumulative[i]}
        if pos is not None:
            record['positionLat']=round(float(latitude)*2**31/180)
            record['positionLong']=round(float(longitude)*2**31/180)
        records.append(record)
    avg_hr=math.floor(sum(hr)/len(hr)+0.5); avg_cad=sum(cadence)/len(cadence)
    set_text(lap,'t:MaximumSpeed',f'{max(speed):.8f}')
    set_text(lap,'t:AverageHeartRateBpm/t:Value',avg_hr)
    set_text(lap,'t:MaximumHeartRateBpm/t:Value',max(hr))
    set_text(lap,'t:Cadence',math.floor(avg_cad+0.5))
    set_text(lap,'t:Extensions/a:LX/a:AvgSpeed',f'{distance/duration:.8f}')
    notes.text=('Screenshot reconstruction approximation: curves extracted from a source image '
        'and interpolated, not original sensor samples. Original GPS/time/distance/duration retained. '
        'Cumulative distance derived from reconstructed speed, scaled to original total. '
        'Native TCX Cadence uses both feet; RunCadence uses half cadence. Step length is '
        'provided in CSV and FIT native fields, not an unverified custom TCX extension. '
        'Source summary: '+json.dumps(summary,ensure_ascii=False))
    pace_check=average_pace_report(distance,duration,summary)
    report={'source_tcx_sha256':hashlib.sha256(data).hexdigest(),
        'curve_anchor_sha256':hashlib.sha256(curves.read_bytes()).hexdigest(),
        'anchor_count':len(anchors),'output_points':len(points),'duration_s':duration,'distance_m':distance,
        'source':'screenshot_reconstruction_approximation','pace_speed_scale_to_total_distance':scale,
        'average_pace_check':pace_check,'native_cadence_fields':len(points),
        'heart_rate':{'mean':sum(hr)/len(hr),'max':max(hr)},
        'cadence':{'mean_spm':avg_cad,'encoded_native_mean_spm':sum(native_cad)/len(native_cad),
                   'encoded_run_mean_spm':2*sum(run_cad)/len(run_cad)},
        'step_length':{'mean_cm':sum(step)/len(step),'max_cm':max(step)},
        'curve_variation':{key:max(v)-min(v)>1e-9 for key,v in series.items()},
        'actual_platform_import_verified':False}
    payload={'records':records,'duration_s':duration,'distance_m':distance,
        'calories':int(lap.findtext('t:Calories',namespaces=NS)),
        'steps':int(lap.findtext('t:Extensions/a:LX/a:Steps',namespaces=NS))
                if lap.find('t:Extensions/a:LX/a:Steps',NS) is not None else None,
        'source':'screenshot reconstruction approximation','average_pace_check':pace_check}
    output.parent.mkdir(parents=True,exist_ok=True)
    write_tcx(root,output)
    with output.with_suffix('.csv').open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    with output.with_suffix('.json').open('x',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
    if fit_input is not None:
        Path(fit_input).parent.mkdir(parents=True,exist_ok=True)
        with Path(fit_input).open('x',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_tcx',type=Path); parser.add_argument('curve_csv',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--summary',type=Path); parser.add_argument('--fit-input',type=Path)
    args=parser.parse_args()
    try:
        summary=json.loads(args.summary.read_text(encoding='utf-8-sig')) if args.summary else {}
        print(json.dumps(apply(args.base_tcx,args.curve_csv,args.output,summary,args.fit_input),ensure_ascii=False,indent=2))
    except (ValueError,OSError,E.ParseError,TypeError,KeyError) as e: parser.exit(2,f'Error: {e}\n')
