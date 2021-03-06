#-*- coding: utf8 -*-

import sys, os
from os.path import dirname
sys.path.insert(0, dirname(dirname(os.path.abspath(__file__))))

import time
import traceback
from datetime import datetime, timedelta, date
import pandas as pd
import simplejson

from yhapi.utils import UTC_time, ifcode_map
from yhapi.mongodb_mod import yh_mongodb
from yhapi.api_logger import yh_api_logger
from yhapi.transaction_api import (trans_short_start,
                                   trans_long_close, trans_long_start,
                                   trans_short_close)

from sell_signals.push_signal import PushSellSignal
from indicator import Indicator, point_prosess_v2, point_prosess_v3
from consts import old_infos_dir_macd, trans_session_dir
from libs.utils import str2day, pre_day

from push_it import push_sig

column_key = ['InsertTime', 'Time', 'Now', 'Open',\
              'High', 'Low', 'CurHold', 'Hold',\
              'Volume', 'BuyVolume', 'SellVolume', 'VolumeRate']


def is_push(new_infos, ifcode, today):
    _file = '%s/%s_%s' % (old_infos_dir_macd, ifcode, today)
    old_infos = []
    if not os.path.isfile(_file): # init
        f = open(_file, 'w')
        try:
            f.write(simplejson.dumps(new_infos))
            f.close()
        except BaseException, e:
            tip = '写信号文件失败'
            push_sig(tip)
            f.close()
            return False
        return True
    else:
        f = open(_file)
        try:
            old_infos = simplejson.loads(f.read())
            f.close()
        except BaseException, e:
            tip = '读信号文件失败'
            push_sig(tip)
            f.close()
            return False
        if len(new_infos) != len(old_infos):
            f = open(_file, 'w')
            try:
                f.write(simplejson.dumps(new_infos))
                f.close()
            except BaseException, e:
                tip = '写信号文件失败'
                push_sig(tip)
                f.close()
                return False
            return True
    return False


def realtime_data(ifcode, code, today, coll):
    pre = pre_day(today)
    _pre_year, _pre_month, _pre_day = pre.year, pre.month, pre.day
    pre_datetime_s1 = datetime(_pre_year, _pre_month, _pre_day, 14, 34, 00)
    pre_datetime_e1 = datetime(_pre_year, _pre_month, _pre_day, 15, 00, 00)

    year, month, day = today.year, today.month, today.day
    datetime_s1 = datetime(year, month, day, 9, 30, 00)
    datetime_e1 = datetime(year, month, day, 11, 30, 00)
    datetime_s2 = datetime(year, month, day, 13, 00, 00)
    datetime_e2 = datetime(year, month, day, 15, 00, 00)
    rows = coll.find({"$or": [{"InsertTime": {"$gte": pre_datetime_s1, '$lt': pre_datetime_e1}},
                              {"InsertTime": {"$gte": datetime_s1, '$lt': datetime_e1}},
                              {"InsertTime": {"$gte": datetime_s2, '$lt': datetime_e2}}]},\
                     {'_id': -1, 'InsertTime': 1, 'Time': 1, 'Now': 1,\
                      'High': 1, 'Open': 1, 'CurHold': 1, 'Hold': 1, 'Low': 1,\
                      'Volume': 1, 'BuyVolume': 1, 'SellVolume': 1, 'VolumeRate':1}).sort('InsertTime')
    rs = []
    for doc in rows:
        tmp = []
        for k in column_key:
            if k in doc:
                tmp.append(doc[k])
        if len(tmp) == 12:
            _t = [tmp[1], tmp[2], tmp[8]]
            rs.append(_t)
    return rs


def get_trans_session(ifcode, today):
    _file = '%s/%s_%s' % (trans_session_dir, ifcode, today)
    f = open(_file)
    try:
        trans_session = int(f.read())
        f.close()
    except BaseException, e:
        f.close()
        push_sig('读取 session 文件失败')
        trans_session = None
    return trans_session


def push_signal(df_list, code, ifcode, today, period_short=12, period_long=26, period_dif=9):
    df = pd.DataFrame(df_list, columns=['time_index', 'price', 'volume'])
    #macd
    macd_df = point_prosess_v3(df, 8)
    macd_df['ma_short'] = Indicator.ewma_metric(period_short, macd_df[['price']], 'price', False)
    macd_df['ma_long'] = Indicator.ewma_metric(period_long, macd_df[['price']], 'price', False)
    macd_df['macd_dif'] = macd_df['ma_short'] - macd_df['ma_long']
    macd_df['macd_dem'] = Indicator.ewma_metric(period_dif, macd_df[['macd_dif']], 'macd_dif')

    sig_infos = PushSellSignal.compare_sig(macd_df, 'macd_dif', 'macd_dem', 14)
    profit_infos = PushSellSignal.profit_infos(sig_infos)

    trans_session = get_trans_session(ifcode, today)

    if is_push(profit_infos, ifcode, today):
        if len(profit_infos) == 1:
            print 'init message~!'
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            info_dict = profit_infos[0]

            theevent = info_dict.get('event', '')
            is_success = 'failed'
            r = 'init info'
            #添加交易接口
            if theevent == '卖出信号':
                r = trans_short_start(trans_session, code, ifcode.upper())
                if 'error_info' not in r:
                    is_success = '交易成功'
            elif theevent == '买入信号':
                r = trans_long_start(trans_session, code, ifcode.upper())
                if 'error_info' not in r:
                    is_success = '交易成功'

            tip = '实盘macd 策略: \n%s, 交易价格: %s; 时间: %s; %s' % (info_dict.get('event', ''),
                                                  info_dict.get('price', 0),
                                                  time_str, is_success)
            push_sig(tip, r)
        elif len(profit_infos) >= 2:
            print 'push message~!'
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            info_dict_1 = profit_infos[-2]
            info_dict_2 = profit_infos[-1]
            _p = info_dict_2.get('price', 0)
            trading_fee = cal_fee(_p)
            _g = info_dict_1.get('gain', 0)
            gain = _g - trading_fee

            theevent = info_dict_2.get('event', '')
            is_success = 'failed'
            r = 'init info'
            #添加交易接口
            if theevent == '卖出信号':
                r1 = trans_short_close(trans_session, code, ifcode.upper())
                r2 = trans_short_start(trans_session, code, ifcode.upper())
                r = r1 + r2
                if ('error_info' not in r1 and 'error_info' not in r2) or ('error_no":"30' in r1 and 'error_info' not in r2):
                    is_success = '交易成功'
            elif theevent == '买入信号':
                r1 = trans_long_close(trans_session, code, ifcode.upper())
                r2 = trans_long_start(trans_session, code, ifcode.upper())
                r = r1 + r2
                if ('error_info' not in r1 and 'error_info' not in r2) or ('error_no":"30' in r1 and 'error_info' not in r2):
                    is_success = '交易成功'
            elif theevent == '收盘平仓':
                _event = profit_infos[-3]
                if _event.get('event', '') == '买入信号':
                    r = trans_short_close(trans_session, code, ifcode.upper())
                    if 'error_info' not in r:
                        is_success = '交易成功'
                elif _event.get('event', '') == '卖出信号':
                    r = trans_long_close(trans_session, code, ifcode.upper())
                    if 'error_info' not in r:
                        is_success = '交易成功'

            tip = '实盘macd 策略: \n%s, 盈利: %s, 实际盈利: %s, 交易费用: %s; \n%s, 交易价格: %s; 时间: %s; %s' % (info_dict_1.get('event', ''),
                                                  _g,
                                                  gain,
                                                  trading_fee,
                                                  info_dict_2.get('event', ''),
                                                  _p,
                                                  time_str,
                                                  is_success)
            push_sig(tip, r)


def cal_fee(price):
    return price*1.0*300/10000*23


def main(ifcode, code, period_short, period_long):
    today = date.today()
    year, month, day = today.year, today.month, today.day
    week = today.weekday()
    if week in [5, 6]:
        push_sig('周六周日不交易')
        return
    datetime_start = datetime(year, month, day, 9, 30, 00)
    datetime_end = datetime(year, month, day, 15, 00, 00)
    datetime_now = datetime.now()

    coll_name = '%s_data_second' % ifcode.lower()
    coll = yh_mongodb[coll_name]

    #df_list = realtime_data(ifcode, code, today, coll)
    #push_signal(df_list, code, ifcode, today, period_short, period_long)

    while True:
        try:
            if datetime_now > datetime_start and datetime_now <= datetime_end:
                df_list = realtime_data(ifcode, code, today, coll)
                #print '*'*20
                #print len(df_list)
                try:
                    push_signal(df_list, code, ifcode, today, period_short, period_long)
                except BaseException, e:
                    print traceback.format_exc()
                    push_sig('push_signal_error', traceback.format_exc())
            else:
                break
        except BaseException, e:
            yh_api_logger.error(traceback.format_exc())
        datetime_now = datetime.now()
        time.sleep(10)


if __name__ == '__main__':
    arg = sys.argv
    ifcode, code, period_short, period_long = arg[1:]
    #ifcode, code = 'IF1509', '040109'
    main(ifcode, code, int(period_short), int(period_long))
