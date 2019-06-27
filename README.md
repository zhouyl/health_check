## 使用说明

### 1、安装 virtualenv

```shell
$ sudo apt-get install python3-pip
$ pip3 install virtualenv
$ cd /workspace/health_check
$ virtualenv --no-site-packages -p /usr/bin/python3 venv
```

### 2、包管理

导入依赖包

```shell
$ venv/bin/pip install --upgrade pip # 升级 pip
$ venv/bin/pip install -r requirements.txt # 生产环境使用这个
$ venv/bin/pip install -r requirements_dev.txt # 开发环境使用这个
```

### 3、创建配置文件

参考配置文件 zaful-app.yml，配置文件仅允许使用 yaml 格式

### 4、运行检查命令

注意：PING 命令的执行，需要 root 权限！！

```
$ sudo venv/bin/python health_check.py -f /workspace/health_check/zaful-app.yml
```
