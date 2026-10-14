#!/usr/bin/env python3
"""Repair a single-lap Mi Fitness GPS-only running TCX using stdlib only."""
import argparse
import bisect
import copy
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

TCX = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'
AX = 'http://www.garmin.com/xmlschemas/ActivityExtension/v2'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
ET.register_namespace('', TCX)
ET.register_namespace('ae', AX)
ET.register_namespace('xsi', XSI)


def tag(name, ns=TCX):
    return '{' + ns + '}' + name


def text(node, name):
    return node.findtext(tag(name))


def add(node, name, value=None, ns=TCX):
    child = ET.SubElement(node, tag(name, ns))
    if value is not None:
        child.text = str(value)
    return child


def date(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp lacks timezone; do not guess local time.')
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def number(value, label, low, high):
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be between {low} and {high}.')
    return value


def haversine(a, b):
    la, lo, lb, lob = map(math.radians, (*a, *b))
    h = math.sin((lb-la)/2)**2 + math.cos(la)*math.cos(lb)*math.sin((lob-lo)/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))


def interpolate(x, xs, ys):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    return ys[i-1] + (ys[i]-ys[i-1])*(x-xs[i-1])/(xs[i]-xs[i-1])


def format_pace(seconds):
    seconds = int(seconds)
    return f'{seconds//60}:{seconds%60:02d}'


def parse_displayed_pace(value):
    if value is None:
        return None
    match = re.fullmatch(r'\s*(\d+)(?::|[\'′分])(\d{1,2})(?:["″秒])?\s*', str(value))
    if not match or int(match[2]) >= 60:
        raise ValueError('Displayed pace must be m:ss or minutes/seconds, e.g. 7:23.')
    return int(match[1])*60+int(match[2])


def average_pace_report(distance, duration, summary=None):
    distance = number(distance, 'distance', 0.01, 1000000)
    duration = number(duration, 'duration', 0.001, 86400)
    summary = summary or {}
    exact = duration*1000/distance
    truncated, nearest = math.floor(exact), math.floor(exact+0.5)
    displayed = {key:parse_displayed_pace(summary.get(key))
                 for key in ('display_average_pace', 'target_average_pace')}
    values = [v for v in displayed.values() if v is not None]
    consistent = bool(values) and all(v in {truncated,nearest} for v in values)
    return {'exact_s_km':exact, 'display_truncated':format_pace(truncated),
            'display_nearest_second':format_pace(nearest),
            'screenshot_display':summary.get('display_average_pace'),
            'target_display':summary.get('target_average_pace'),
            'classification':('consistent_with_integer_display_rounding' if consistent
                              else 'requires_investigation' if values else 'not_compared'),
            'platform_rounding_rule_verified':False,
            'distance_and_duration_preserved':True,
            'pace_shift_applied_s':0}


def write_tcx(root, output):
    """Serialize common ns3 prefix without relying on ElementTree private APIs."""
    ET.indent(root,space='  ')
    data = ET.tostring(root,encoding='utf-8',xml_declaration=True)
    data = data.replace(b'xmlns:ae=',b'xmlns:ns3=').replace(b'<ae:',b'<ns3:').replace(b'</ae:',b'</ns3:')
    with Path(output).open('xb') as f:
        f.write(data)


def repair(source, output, summary=None, heart_rate=None, cadence=None):
    source, output = Path(source), Path(output)
    summary = dict(summary or {})
    targets = [output, output.with_suffix('.csv'), output.with_suffix('.json')]
    if source.resolve() in [p.resolve() for p in targets] or any(p.exists() for p in targets):
        raise ValueError('Refusing to overwrite input or existing output files.')
    raw = source.read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD and entity declarations are not supported.')
    root = ET.fromstring(raw)
    if root.tag != tag('TrainingCenterDatabase'):
        raise ValueError('Expected TCX v2 TrainingCenterDatabase.')
    activities = root.findall(tag('Activities') + '/' + tag('Activity'))
    if len(activities) != 1:
        raise ValueError('Inspect multiple activities manually; this helper supports one activity.')
    activity = activities[0]
    if activity.get('Sport') not in ('', 'Running', None):
        raise ValueError('This helper supports running only.')
    laps = activity.findall(tag('Lap'))
    if len(laps) != 1 or len(laps[0].findall(tag('Track'))) != 1:
        raise ValueError('Inspect multiple laps or tracks manually; do not bridge pauses.')
    lap = laps[0]
    allowed_lap = {'TotalTimeSeconds','DistanceMeters','Calories','HeartRateBpm','AverageSpeed','Steps','Track'}
    if any(e.tag not in {tag(n) for n in allowed_lap} for e in lap):
        raise ValueError('Not the supported GPS-only Mi Fitness lap layout; inspect before processing.')
    if any(e.tag not in {tag('Id'),tag('Calories'),tag('Lap')} for e in activity):
        raise ValueError('Existing activity metadata requires manual inspection.')
    points = lap.find(tag('Track')).findall(tag('Trackpoint'))
    if len(points) < 2:
        raise ValueError('At least two GPS samples are required.')
    if any(e.tag not in {tag('Time'),tag('Position'),tag('AltitudeMeters')} for p in points for e in p):
        raise ValueError('Existing sample metrics found; preserve them instead of replacing them.')
    duration = number(text(lap, 'TotalTimeSeconds'), 'duration', 1, 86400)
    distance = number(text(lap, 'DistanceMeters'), 'distance', 0.01, 1000000)
    start_text = text(activity, 'Id')
    start = date(start_text)
    elapsed = [(date(text(p, 'Time'))-start).total_seconds() for p in points]
    gaps = [b-a for a,b in zip(elapsed, elapsed[1:])]
    if any(g <= 0 for g in gaps):
        raise ValueError('Duplicate or decreasing timestamps; inspect manually.')
    if elapsed[0] < 0 or elapsed[0] > 5 or abs(elapsed[-1]-duration) > 0.001:
        raise ValueError('Activity start/end differs from duration; inspect pauses and timezone first.')
    if max(gaps) > 5:
        raise ValueError('GPS gap exceeds 5 seconds; inspect pauses rather than fabricate a continuous run.')
    if duration != int(duration) or any(t != int(t) for t in elapsed):
        raise ValueError('Subsecond timing requires manual inspection; timestamps will not be rounded.')
    positions = []
    for p in points:
        pos = p.find(tag('Position'))
        if pos is None:
            raise ValueError('Original trackpoint lacks GPS.')
        positions.append((number(text(pos,'LatitudeDegrees'),'latitude',-90,90),
                          number(text(pos,'LongitudeDegrees'),'longitude',-180,180)))
    cumulative = [0.0]
    for a,b,dt in zip(positions,positions[1:],gaps):
        segment = haversine(a,b)
        if segment/dt > 12:
            raise ValueError('Implausible GPS jump exceeds 12 m/s; inspect before distance calibration.')
        cumulative.append(cumulative[-1]+segment)
    if cumulative[-1] <= 0:
        raise ValueError('GPS path has zero distance.')
    scale = distance/cumulative[-1]
    if not 0.5 <= scale <= 2:
        raise ValueError('GPS length differs greatly from reported distance; inspect before calibration.')
    calibrated = [v*scale for v in cumulative]
    seconds = list(range(int(duration)+1))
    distances = [interpolate(t,elapsed,calibrated) for t in seconds]
    speeds = []
    for t in seconds:
        left, right = max(0,t-5), min(duration,t+5)
        speeds.append((interpolate(right,elapsed,calibrated)-interpolate(left,elapsed,calibrated))/(right-left))
    steps_raw = text(lap, 'Steps')
    steps = int(number(steps_raw,'steps',0,65535)) if steps_raw is not None else None
    if steps_raw is not None and float(steps_raw) != steps:
        raise ValueError('Steps must be an integer.')
    if heart_rate is not None:
        hr_value, hr_source = heart_rate, 'explicit override'
    elif summary.get('average_hr_bpm') is not None:
        hr_value, hr_source = summary['average_hr_bpm'], 'screenshot summary'
    else:
        hr_value, hr_source = text(lap,'HeartRateBpm'), 'original lap average'
    hr = None if hr_value is None else int(number(hr_value,'heart rate',1,255))
    if hr_value is not None and float(hr_value) != hr:
        raise ValueError('Heart rate must be an integer.')
    if cadence is not None:
        cad_value, cad_source = cadence, 'explicit override'
    elif summary.get('average_cadence_spm') is not None:
        cad_value, cad_source = summary['average_cadence_spm'], 'screenshot summary'
    elif steps:
        cad_value, cad_source = steps*60/duration, 'total steps / total duration'
    else:
        cad_value, cad_source = None, 'unavailable'
    cad = None if cad_value is None else number(cad_value,'cadence (both feet)',1,254)
    # Garmin RunCadence uses whole cycles/minute. Error diffusion avoids 179 -> 180 bias.
    run_cadences = [math.floor((i+1)*cad/2 + 1e-8)-math.floor(i*cad/2 + 1e-8)
                    for i in range(len(seconds))] if cad else [None]*len(seconds)
    original_map = {int(t):p for t,p in zip(elapsed,points)}
    out_root = ET.Element(tag('TrainingCenterDatabase'), {
        tag('schemaLocation',XSI): TCX+' https://www8.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd '+AX+' https://www8.garmin.com/xmlschemas/ActivityExtensionv2.xsd'})
    out_activity = ET.SubElement(add(out_root,'Activities'),tag('Activity'),{'Sport':'Running'})
    add(out_activity,'Id',start_text)
    out_lap = ET.SubElement(out_activity,tag('Lap'),{'StartTime':start_text})
    add(out_lap,'TotalTimeSeconds',text(lap,'TotalTimeSeconds'))
    add(out_lap,'DistanceMeters',text(lap,'DistanceMeters'))
    add(out_lap,'MaximumSpeed',f'{max(speeds):.6f}')
    calorie_text = text(lap,'Calories')
    if calorie_text is None:
        raise ValueError('Lap calories missing; inspect before adding a required value.')
    calories = number(calorie_text,'calories',0,65535)
    if int(calories) != calories:
        raise ValueError('Calories must be an integer.')
    add(out_lap,'Calories',int(calories))
    if hr is not None:
        add(add(out_lap,'AverageHeartRateBpm'),'Value',hr)
        add(add(out_lap,'MaximumHeartRateBpm'),'Value',hr)
    add(out_lap,'Intensity','Active')
    if cad is not None:
        add(out_lap,'Cadence',math.floor(cad+0.5))
    add(out_lap,'TriggerMethod','Manual')
    track = add(out_lap,'Track')
    rows = []
    for i,t in enumerate(seconds):
        original = original_map.get(t)
        p = add(track,'Trackpoint')
        add(p,'Time',text(original,'Time') if original is not None else stamp(start+timedelta(seconds=t)))
        if original is not None:
            p.append(copy.deepcopy(original.find(tag('Position'))))
            altitude = original.find(tag('AltitudeMeters'))
            if altitude is not None:
                p.append(copy.deepcopy(altitude))
        add(p,'DistanceMeters',f'{distances[i]:.6f}')
        if hr is not None:
            add(add(p,'HeartRateBpm'),'Value',hr)
        if cad is not None:
            add(p,'Cadence',math.floor(cad+0.5))
        tpx = add(add(p,'Extensions'),'TPX',ns=AX)
        add(tpx,'Speed',f'{speeds[i]:.6f}',ns=AX)
        if cad is not None:
            add(tpx,'RunCadence',run_cadences[i],ns=AX)
        lat,lon = positions[bisect.bisect_left(elapsed,t)] if original is not None else ('','')
        rows.append({'time_utc':text(p,'Time'),'elapsed_s':t,'latitude':lat,'longitude':lon,
                     'gps_original':int(original is not None),'distance_m':f'{distances[i]:.6f}',
                     'speed_m_s_gps_estimate':f'{speeds[i]:.6f}',
                     'pace_s_km_gps_estimate':f'{1000/speeds[i]:.3f}' if speeds[i]>0 else '',
                     'hr_bpm_constant_fill':hr if hr is not None else '',
                     'cadence_spm_constant_fill':f'{cad:.6f}' if cad is not None else '',
                     'tcx_run_cadence_cycles_min':run_cadences[i] if cad is not None else '',
                     'step_length_cm_estimate':f'{speeds[i]*6000/cad:.3f}' if cad else ''})
    lx = add(add(out_lap,'Extensions'),'LX',ns=AX)
    add(lx,'AvgSpeed',f'{distance/duration:.6f}',ns=AX)
    if steps is not None:
        add(lx,'Steps',steps,ns=AX)
    report = {
        'input_sha256':hashlib.sha256(raw).hexdigest(), 'original_start_utc':start_text,
        'duration_s':duration,'distance_m':distance,'active_calories_kcal':calories,
        'original_activity_calories':text(activity,'Calories'),'steps':steps,
        'original_gps_points':len(points),'output_points':len(seconds),
        'inserted_points_without_gps':len(seconds)-len(points),'gps_coordinates_modified':False,
        'gps_path_length_m':cumulative[-1],'gps_distance_scale':scale,
        'speed_method':'scaled GPS cumulative distance, centered 10-second secant, m/s',
        'heart_rate':{'fixed_bpm':hr,'source':hr_source,'real_sensor_curve_recovered':False},
        'cadence':{'fixed_spm_both_feet':cad,'source':cad_source,
                   'tcx_mean_spm_both_feet':2*sum(v or 0 for v in run_cadences)/len(seconds) if cad else None,
                   'encoding':'integer half cadence, error-diffusion quantization',
                   'real_sensor_curve_recovered':False},
        'step_length_from_totals_cm':distance*100/steps if steps else None,
        'cadence_from_totals_spm':steps*60/duration if steps else None,
        'screenshot_summary':summary,
        'average_pace_check':average_pace_report(distance,duration,summary),
        'limitations':['Screenshot maxima are summary observations only; not encoded as recovered sensor maxima.',
                      'Step length CSV is speed/cadence derived; screenshot mean is retained separately.',
                      'No invented altitude, power, ground contact, VO2max or training-effect time series.',
                      'Importer display and import success require target-platform verification.']}
    notes = ('Mi Fitness repair: original GPS/time retained. Missing HR/cadence replaced by fixed averages, '
             'not recovered sensor measurements. Speed/distance estimated from GPS and scaled to lap distance. '
             'RunCadence is integer half cadence; quantization is not a real cadence fluctuation. '
             'Original/screenshot summary: '+json.dumps({'original_activity_calories':text(activity,'Calories'),
                'steps':steps,'fixed_hr_bpm':hr,'fixed_cadence_spm':cad,'screenshot':summary},ensure_ascii=False))
    add(out_activity,'Notes',notes)
    output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation guards against accidental overwrites, including a second execution.
    write_tcx(out_root,output)
    with targets[1].open('x',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    with targets[2].open('x',encoding='utf-8') as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--summary',type=Path,help='Screenshot summary JSON; not sensor time series')
    parser.add_argument('--heart-rate',type=int)
    parser.add_argument('--cadence',type=float,help='Total steps/minute, both feet')
    args = parser.parse_args()
    try:
        summary = json.loads(args.summary.read_text(encoding='utf-8-sig')) if args.summary else {}
        report = repair(args.input,args.output,summary,args.heart_rate,args.cadence)
    except (ValueError, OSError, ET.ParseError, TypeError) as error:
        parser.exit(2,f'Error: {error}\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
