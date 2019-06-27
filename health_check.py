# -*- coding: utf-8 -*-

import argparse
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

import ping3
import pymemcache.client.base
import pymysql
import redis
import redis.sentinel
import stringcase
import yaml

VERSION = '0.0.1'
AUTHOR = 'zhouyl (81438567@qq.com)'

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s][%(levelname)s] - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

log = logging.getLogger('health_check')

ping3.EXCEPTIONS = True


def human_size(size):
    for unit in ['Bytes', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024.0:
            return "%3.1f %s" % (size, unit)
        size /= 1024.0
    return "%3.1f %s" % (size, 'PB')


def no_zero_div(n1, n2):
    try:
        return n1 / n2
    except ZeroDivisionError:
        return 0


class BaseRule:

    def check(self):
        pass


class HttpRule(BaseRule):

    def __init__(self, url, maxtime=3, requests=1, intervals=0.5):
        self._url = str(url)
        self._maxtime = float(maxtime)
        self._requests = int(requests)
        self._intervals = float(intervals)

    def check(self):
        log.info("检查 HTTP 连接 - URL: %s, 限定时间: %ds, 请求次数: %d",
                 self._url, self._maxtime, self._requests)

        for n in range(self._requests):
            try:
                if n > 0:
                    time.sleep(self._intervals)
                start = time.time()
                r = urllib.request.urlopen(self._url, timeout=self._maxtime)
                if r.getcode() != 200:
                    log.error('[第 %d 次请求][%.6fs] 发生错误: 状态码为 %d,',
                              n + 1, time.time() - start, r.getcode())
                else:
                    log.info("[第 %d 次请求][%.6fs] 请求响应成功",
                             n + 1, time.time() - start, )
            except urllib.error.URLError as e:
                log.error('[第 %d 次请求][%.6fs] 发生错误: %s - %s',
                          n + 1, time.time() - start, e.__class__, e)


class PingRule(BaseRule):

    def __init__(self, host, timeout=4, ttl=64, seq=0, size=56):
        self._host = str(host)
        self._timeout = int(timeout)
        self._ttl = int(ttl)
        self._seq = int(seq)
        self._size = int(size)

    def check(self):
        log.info('尝试 PING 主机: %s', self._host)

        try:
            delay = ping3.ping(
                dest_addr=self._host,
                timeout=self._timeout,
                ttl=self._ttl,
                seq=self._seq,
                size=self._size)
            if delay is None:
                log.error('获取 PING 数据失败！')
            else:
                log.info('PING 成功，耗时: %.6fs', delay)
        except ping3.errors.PingError as e:
            log.error('发生错误: %s - %s', e.__class__, e)


class SocketRule(BaseRule):

    def __init__(self, host, port, timeout=10):
        self._host = str(host)
        self._port = int(port)
        self._timeout = int(timeout)

    def check(self):
        log.info('SOCKET 连接检查: %s:%d', self._host, self._port)

        try:
            s = socket.socket()
            s.connect((self._host, self._port))
            log.info('SOCKET TCP 连接成功！')
            s.close()
        except ConnectionError as e:
            log.error('SOCKET TCP 连接失败: %s - %s', e.__class__, e)


class MemcacheRule(BaseRule):

    def __init__(self, host, port):
        self._host = str(host)
        self._port = int(port)

    def check(self):
        log.info('检查 Memcache 连接： %s:%d', self._host, self._port)

        try:
            mc = pymemcache.client.base.Client((self._host, self._port))

            stats = dict()
            for k, v in mc.stats().items():
                stats[k.decode('utf8')] = v.decode('utf8') if isinstance(v, bytes) else v
            mc.close()

            log.info('> Memcache 版本: %s, 进程 ID: %s',
                     stats['version'], stats['pid'])

            log.info('> 当前连接数: %s，历史连接总数: %s',
                     stats['curr_connections'], stats['total_connections'])

            log.info('> 已存储 %d 个对象，占用空间 %s, 命中率: %.2f%%',
                     stats['curr_items'], human_size(stats['bytes']),
                     no_zero_div(stats['get_hits'], stats['get_hits'] + stats['get_misses']) * 100)

        except Exception as e:
            log.error('连接 Memcache 失败: %s - %s', e.__class__, e)


class RedisRule(BaseRule):

    def __init__(self, host='127.0.0.1', port=6379, *args, **kwargs):
        self._host = str(host)
        self._port = int(port)
        self._args = args
        self._kwargs = kwargs

    def _redis_info(self, connection):
        connection.send_command('INFO')
        return redis.client.parse_info(connection.read_response())

    def _log_redis_info(self, info):
        log.info('> Redis 版本: %s, 进程 ID: %d',
                 info['redis_version'], info['process_id'])

        log.info('> 内存占用: %s, 消耗峰值: %s',
                 info['used_memory_human'], info['used_memory_peak_human'])

        log.info('> 系统 CPU 占用: %d, 用户 CPU 占用: %d',
                 info['used_cpu_sys'], info['used_cpu_user'])

        log.info('> 已连接客户端: %d, 连接请求数: %d',
                 info['connected_clients'], info['total_connections_received'])

        log.info('> 已执行命令: %d, 每秒执行: %d',
                 info['total_commands_processed'], info['instantaneous_ops_per_sec'])

        log.info('> 等待阻塞命令客户端: %d, 被拒绝的连接请求: %d',
                 info['blocked_clients'], info['rejected_connections'])

        log.info('> 查找数据库键成功: %d, 失败: %d',
                 info['keyspace_hits'], info['keyspace_misses'])

    def check(self):
        log.info('检查 Redis 连接: %s:%d', self._host, self._port)

        try:
            conn = redis.Connection(self._host, self._port, *self._args, **self._kwargs)
            conn.connect()
            info = self._redis_info(conn)
            conn.disconnect()
            self._log_redis_info(info)
        except redis.exceptions.RedisError as e:
            log.error('连接 Redis 失败: %s - %s', e.__class__, e)


class RedisSentinelRule(RedisRule):

    def __init__(self, master, password='', sentinels=()):
        self._master = str(master)
        self._password = str(password)
        self._sentinels = list(sentinels)

    def check(self):
        log.info('检查 Redis Sentinel 连接: %s', self._master)

        try:
            sentinel = redis.sentinel.Sentinel(
                [s.split(':') for s in self._sentinels],
                password=self._password, socket_timeout=0.1)

            log.info('> master: %s', sentinel.discover_master(self._master))
            log.info('> slaves: %s', sentinel.discover_slaves(self._master))
            log.info('尝试获取 master 节点状态信息...')

            master = sentinel.master_for(self._master, socket_timeout=0.1)
            self._log_redis_info(master.info())
        except redis.sentinel.MasterNotFoundError as e:
            log.error('检查 Redis Sentinel 出错: %s - %s', e.__class__, e)


class MysqlRule(BaseRule):

    def __init__(self, host='localhost', port=3306, user='root', password='',
                 database='', connect_timeout=10, init_command='select 1',
                 performance_seconds=0):
        self._host = str(host)
        self._port = int(port)
        self._user = str(user)
        self._password = str(password)
        self._database = str(database)
        self._connect_timeout = int(connect_timeout)
        self._init_command = str(init_command)
        self._performance_seconds = int(performance_seconds)

    def _connect(self, host, show_status=False):
        try:
            conn = pymysql.connect(
                host=host,
                port=self._port,
                user=self._user,
                password=self._password,
                database=self._database,
                connect_timeout=self._connect_timeout,
                init_command=self._init_command)
            if show_status:
                self._show_status(conn)
            else:
                log.info('[%s]: 连接成功', host)
            conn.close()
        except pymysql.err.MySQLError as e:
            log.error('[%s] 连接失败: %s - %s', host, e.__class__, e)

    def _show_info(self, conn, sql):
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(sql)

        data = dict()
        for row in cursor.fetchall():
            # 对数字/浮点类型值进行自动转换
            # 以便于相关的代码编写更便利
            if re.match(r'^\-?\d+$', row['Value']):
                data[row['Variable_name']] = int(row['Value'])
            elif re.match(r'^\-?\d+\.\d+$', row['Value']):
                data[row['Variable_name']] = float(row['Value'])
            else:
                data[row['Variable_name']] = str(row['Value'])

        return data

    def _slave_info(self, conn):
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute('SHOW SLAVE STATUS')
        return cursor.fetchall()

    def _show_status(self, conn):
        secs = self._performance_seconds
        vars = self._show_info(conn, 'SHOW VARIABLES')
        s1 = self._show_info(conn, 'SHOW GLOBAL STATUS')

        r = s1.get('Com_select', 0) + s1.get('Qcache_hits', 0)
        w = s1.get('Com_insert', 0) + s1.get('Com_update', 0) + \
            s1.get('Com_delete', 0) + s1.get('Com_replace', 0)
        log.info('> 读写比例: %d reads / %d writes * 100 = %.2f%%',
                 r, w, no_zero_div(r, w))

        log.info('> Key Buffer 命中率: %.2f%% read hits / %.2f%% write hits',
                 (1 - no_zero_div(s1['Key_reads'], s1['Key_read_requests'])) * 100,
                 (1 - no_zero_div(s1['Key_writes'], s1['Key_write_requests'])) * 100)

        log.info('> InnoDB Buffer 命中率: %.2f%%',
                 (1 - no_zero_div(s1['Innodb_buffer_pool_reads'],
                                  s1['Innodb_buffer_pool_read_requests'])) * 100)

        log.info('> Thread Cache 命中率: %.2f%% read hits',
                 (1 - no_zero_div(s1['Threads_created'], s1['Connections'])) * 100)

        log.info('> 最大连接数限制(max_connections): %d', vars['max_connections'])
        log.info('> 当前开放连接(Threads_connected): %d', s1['Threads_connected'])
        log.info('> 当前运行连接(Threads_running): %d', s1['Threads_running'])
        log.info('> 服务器错误导致的失败连接(Connection_errors_internal): %d',
                 s1['Connection_errors_internal'])
        log.info('> 试图连接到服务器失败的连接(Aborted_connects): %d', s1['Aborted_connects'])
        log.info('> 未正确关闭导致连接中断的客户端(Aborted_clients): %d', s1['Aborted_clients'])
        log.info('> 超出最大连接数限制失败的连接(Connection_errors_max_connections): %d',
                 s1['Connection_errors_max_connections'])

        # try:
        #     slaves = self._slave_info(conn)
        #     # TODO: print slaves info
        # except pymysql.err.MySQLError as e:
        #     log.error('查询 SLAVE 信息失败: %s - %s', e.__class__, e)

        if secs > 0:
            log.info('等待 %d 秒钟，以监测性能数据...', secs)

            time.sleep(secs)
            s2 = self._show_info(conn, 'SHOW GLOBAL STATUS')

            log.info('> 每秒查询量(QPS): %.2f | 每秒事务数(TPS): %.2f',
                     (s2['Queries'] - s1['Queries']) / secs,
                     ((s2['Com_commit'] - s1['Com_commit']) +
                      (s2['Com_rollback'] - s1['Com_rollback'])) / secs)
            log.info('> 每秒读取数据: %s | 每秒写入数据: %s',
                     human_size((s2['Innodb_data_read'] - s1['Innodb_data_read']) / secs),
                     human_size((s2['Innodb_data_written'] - s1['Innodb_data_written']) / secs))
            log.info('> 每秒接收数据: %s | 每秒发送数据: %s',
                     human_size((s2['Bytes_received'] - s1['Bytes_received']) / secs),
                     human_size((s2['Bytes_sent'] - s1['Bytes_sent']) / secs))

    def check(self):
        log.info("检查 MYSQL 连接 - HOST: %s:%d, USER: %s, DBNAME: %s",
                 self._host, self._port, self._user, self._database)

        self._connect(self._host, True)


class HealthCheck:

    def __init__(self, config_file):
        log.info("加载配置文件: %s", config_file)
        self._config = self._load_yaml_config(config_file)

    def _load_yaml_config(self, config_file):
        if not os.path.isfile(config_file):
            raise IOError('配置文件不存在: "%s"' % config_file)

        with open(config_file, 'r') as f:
            return yaml.load(f.read(), yaml.CLoader)

    def _get_rule_class(self, rule_name):
        try:
            m = sys.modules[__name__]
            c = stringcase.pascalcase(rule_name) + 'Rule'

            if not hasattr(m, c):
                log.error("无效的规则类型: %s", c)

            return getattr(m, c)
        except TypeError as e:
            log.error("配置文件参数错误: %s", e)

        return None

    def _check(self, rule, name, options):
        log.info("规则检查: [%s - %s]", rule, name)

        cls = self._get_rule_class(rule)
        if cls is not None:
            cls(**options).check()

    def do_check(self):
        for r in self._config:
            for n in self._config[r]:
                log.info("-" * 80)
                self._check(r, n, self._config[r][n])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='项目健康状态检查脚本',
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('-f', '--config-file', action='store',
                        dest='config', type=str, required=True, help='配置文件')

    parsed_args = parser.parse_args()

    # try:
    log.info("开始环境健康检查....")
    HealthCheck(parsed_args.config).do_check()
    # except Exception as e:
    #     log.error('%s: %s', e.__class__, e)
