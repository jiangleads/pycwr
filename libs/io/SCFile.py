# -*- coding: utf-8 -*-
import struct
import numpy as np
from BaseDataProtocol.SCProtocol import dtype_sc
from util import _prepare_for_read, _unpack_from_buf, get_radar_info
import time
import pandas as pd
import datetime
from libs.core.NRadar import NuistRadar

class SCBaseData(object):
    """
    解码SC/CD 1.0的数据格式
    """
    def __init__(self, filename):
        super(SCBaseData, self).__init__()
        self.filename = filename
        self.fid = _prepare_for_read(self.filename) ##判断是否是压缩文件
        buf_header = self.fid.read(dtype_sc.BaseDataHeaderSize) ##取出header的buf
        self.header = self._parse_BaseDataHeader(buf_header)
        self.MaxV = self.header['LayerParam']['MaxV'][0]/100. ##??可能会存在问题，如果不同仰角采用不用的PRF
        self._check_sc_basedata()
        self.fid.seek(dtype_sc.BaseDataHeaderSize, 0) ##移动到径向数据的位置
        self.radial = self._parse_radial()
        self.fid.close()

    def _check_sc_basedata(self):
        """检查雷达数据是否完整"""
        buf_radial_data = self.fid.read()
        assert len(buf_radial_data) == self.nrays * dtype_sc.PerRadialSize, "SC basedata size has problems!"
        return

    def _parse_BaseDataHeader(self, buf_header):
        """
        :param buf_header: 只包含头文件的buf
        :return:
        """
        BaseDataHeader_dict = {}
        ##解码雷达站点信息
        BaseDataHeader_dict['RadarSite'], _ = _unpack_from_buf(buf_header,\
        dtype_sc.RadarSitePos,dtype_sc.BaseDataHeader['RadarSite'])
        ##解码雷达性能参数
        BaseDataHeader_dict['RadarPerformanceParam'], _ = _unpack_from_buf(buf_header,\
        dtype_sc.RadarPerformanceParamPos,dtype_sc.BaseDataHeader['RadarPerformanceParam'])
        ##解码观测参数
        BaseDataHeader_dict['RadarObserationParam_1'], _ = _unpack_from_buf(buf_header, \
        dtype_sc.RadarObserationParamPos_1, dtype_sc.BaseDataHeader['RadarObserationParam_1'])
        ##目前仅支持VOL扫描格式
        assert BaseDataHeader_dict['RadarObserationParam_1']['stype'] > 100, "only vol support!"
        self.nsweeps = BaseDataHeader_dict['RadarObserationParam_1']['stype'] - 100
        ##解码不同仰角的观测参数
        BaseDataHeader_dict['LayerParam'] = np.frombuffer(buf_header, \
        dtype_sc.BaseDataHeader['LayerParamX30'],count=self.nsweeps, offset=dtype_sc.LayerParamPos)
        ##解码其余一些观测参数
        BaseDataHeader_dict['RadarObserationParam_2'], _ = _unpack_from_buf(buf_header,\
            dtype_sc.RadarObserationParamPos_2, dtype_sc.BaseDataHeader['RadarObserationParam_2'])
        self.nrays = np.sum(BaseDataHeader_dict['LayerParam']['recordnumber'])
        self.sweep_end_ray_index_add1 = np.cumsum(BaseDataHeader_dict['LayerParam']['recordnumber']) ##python格式的结束
        self.sweep_start_ray_index = self.sweep_end_ray_index_add1 - BaseDataHeader_dict['LayerParam']['recordnumber']
        return BaseDataHeader_dict

    def _parse_radial(self):
        radial = []
        for isweep in range(self.nsweeps):
            MaxV = self.header['LayerParam']['MaxV'][isweep] / 100.
            for _ in range(self.header['LayerParam']['recordnumber'][isweep]):
                buf_radial = self.fid.read(dtype_sc.PerRadialSize)
                radial.append(self._parse_radial_single(buf_radial, 0, MaxV, -1))
        return radial

    def _parse_radial_single(self, buf_radial, start_pos, MaxV, num_bins=-1):
        """
        :param buf_radial:
        :param start_pos: 开始的pos
        :param num_bins: 库数
        :return:
        """
        radial_dict = {}
        radial_dict_tmp, size_tmp = _unpack_from_buf(buf_radial, start_pos, dtype_sc.RadialHeader())
        radial_dict.update(radial_dict_tmp)
        radial_dict['fields'] = {}
        RadialData = np.frombuffer(buf_radial, dtype_sc.RadialData(),\
                                         count=num_bins, offset=start_pos+size_tmp)
        radial_dict['fields']['dBZ'] = np.where(RadialData['dBZ'] != 0,\
                            (RadialData['dBZ'].astype(int) - 64)/2., np.nan).astype(np.float32)
        radial_dict['fields']['dBT'] = np.where(RadialData['dBT'] != 0, \
                        (RadialData['dBT'].astype(int) - 64) / 2., np.nan).astype(np.float32)
        radial_dict['fields']['V'] = np.where(RadialData['V'] != 0, \
                       MaxV * (RadialData['V'].astype(int) - 128) / 128., np.nan).astype(np.float32)
        radial_dict['fields']['W'] = np.where(RadialData['W'] != 0, \
                        MaxV * RadialData['W'].astype(int) /256., np.nan).astype(np.float32)
        return radial_dict

    def get_nyquist_velocity(self):
        """get nyquist vel per ray
        获取每根径向的不模糊速度
        :return:(nRays)
        """
        nyquist_velocity = np.concatenate([np.array([self.header['LayerParam']['MaxV'][isweep] / \
                          100.] * self.header['LayerParam']['recordnumber'][isweep]) for \
                            isweep in range(self.nsweeps)])
        return nyquist_velocity.astype(np.float32)
    def get_unambiguous_range(self):
        """
        获取每根径向的不模糊距离
        :return:(nRays)
        """
        return np.concatenate([np.array([self.header['LayerParam']['MaxL'][isweep] *10 \
                            ] * self.header['LayerParam']['recordnumber'][isweep]) for \
                            isweep in range(self.nsweeps)])

    def get_scan_time(self):
        """
        获取每根径向的扫描时间
        :return:(nRays)
        """
        Start_params = self.header['RadarObserationParam_1']
        End_params = self.header['RadarObserationParam_2']
        start_time = datetime.datetime(year=Start_params['syear'], month=Start_params['smonth'],
                                       day=Start_params['sday'], hour=Start_params['shour'],
                                       minute=Start_params['sminute'], second=Start_params['ssecond'])
        end_time = datetime.datetime(year=End_params['Eyear'], month=End_params['Emonth'],
                                       day=End_params['Eday'], hour=End_params['Ehour'],
                                       minute=End_params['Eminute'], second=End_params['Esecond'])
        return pd.date_range(start_time, end_time, periods=self.nrays).to_pydatetime() - datetime.timedelta(hours=8)

    def get_sweep_end_ray_index(self):
        """
        获取每个sweep的结束的index，包含在内
        :return:(nsweep)
        """
        return self.sweep_end_ray_index_add1 - 1
    def get_sweep_start_ray_index(self):
        """
        获取每个sweep的开始的index
        :return:(nsweep)
        """
        return self.sweep_start_ray_index

    def get_rays_per_sweep(self):
        """
        获取每个sweep的径向数
        :return:(nsweep)
        """
        return self.sweep_end_ray_index_add1 - self.sweep_start_ray_index

    def get_azimuth(self):
        """
        获取每根径向的方位角
        :return:(nRays)
        """
        return np.array([(iray['sStrAz'] + iray['sEndAz'])*180./65536 for iray in self.radial])

    def get_elevation(self):
        """
        获取每根径向的仰角
        :return: (nRays)
        """
        return np.array([(iray['sStrEl'] + iray['sEndEl'])*180./65536 for iray in self.radial])

    def get_latitude_longitude_altitude_frequency(self):
        """
        获取经纬度高度，雷达频率
        :return:lat, lon, alt, frequency
        """
        return self.header['RadarSite']['latitudevalue']/100., self.header['RadarSite']['longitudevalue']/100., \
               self.header['RadarSite']['height'] / 1000., 2.765

    def get_scan_type(self):
        """
        获取扫描的类型
        :return:
        """
        if self.header['RadarObserationParam_1']['stype'] == 1:
            return "rhi"
        else:
            return "ppi"

class SC2NRadar(object):
    """到NusitRadar object 的桥梁"""

    def __init__(self, SC):
        self.SC = SC
        self.radial = self.SC.radial
        self.azimuth = self.get_azimuth()
        self.elevation = self.get_elevation()
        self.sweep_start_ray_index = self.get_sweep_start_ray_index()
        self.sweep_end_ray_index = self.get_sweep_end_ray_index()
        self.nrays = self.SC.nrays
        self.nsweeps = self.SC.nsweeps
        self.scan_type = self.SC.get_scan_type()
        self.latitude, self.longitude, self.altitude, self.frequency = \
            self.SC.get_latitude_longitude_altitude_frequency()
        self.bins_per_sweep = self.get_nbins_per_sweep()
        self.max_bins = self.bins_per_sweep.max()
        self.range = self.get_range_per_radial(self.max_bins)
        self.fields = self._get_fields()

    def get_azimuth(self):
        """
        获取每根径向的方位角
        :return:(nRays)
        """
        return self.SC.get_azimuth()

    def get_elevation(self):
        """
        获取每根径向的仰角
        :return: (nRays)
        """
        return self.SC.get_elevation()

    def get_rays_per_sweep(self):
        """
        获取每个sweep的径向数
        :return:(nsweep)
        """
        return (self.SC.header['LayerParam']['recordnumber']).astype(int)

    def get_scan_time(self):
        """
        获取每根径向的扫描时间
        :return:(nRays)
        """
        return self.SC.get_scan_time()

    def get_nyquist_velocity(self):
        """get nyquist vel per ray
        获取每根径向的不模糊速度
        :return:(nRays)
        """
        return self.SC.get_nyquist_velocity()

    def get_unambiguous_range(self):
        """
        获取每根径向的不模糊距离
        :return:(nRays)
        """
        return self.SC.get_unambiguous_range()

    def get_sweep_end_ray_index(self):
        """
        获取每个sweep的结束的index，包含在内
        :return:(nsweep)
        """
        return self.SC.sweep_end_ray_index_add1 - 1

    def get_sweep_start_ray_index(self):
        """
        获取每个sweep的开始的index
        :return:(nsweep)
        """
        return self.SC.sweep_start_ray_index

    def get_nbins_per_sweep(self):
        """
        确定每个sweep V探测的库数
        :return:
        """
        return np.array([self.radial[idx]['fields']['dBZ'].size for idx in self.sweep_start_ray_index])

    def get_range_per_radial(self, length):
        """
        确定径向每个库的距离
        :param length:
        :return:
        """
        Resolution = self.SC.header['LayerParam']['binWidth'][0]/10.
        return np.linspace(Resolution, Resolution * length, length)

    def _get_fields(self):
        """将所有的field的数据提取出来"""
        fields = {}
        field_keys = self.radial[0]['fields'].keys()
        for ikey in field_keys:
            fields[ikey] = np.array([(iray['fields'][ikey]).ravel() for iray in self.radial])
        return fields

    def get_NRadar_nyquist_speed(self):
        """array shape (nsweeps)"""
        return self.SC.header['LayerParam']['MaxV'] / 100.

    def get_NRadar_unambiguous_range(self):
        """array shape (nsweeps)"""
        return self.SC.header['LayerParam']['MaxL'] * 10.

    def get_fixed_angle(self):
        return self.SC.header['LayerParam']['Swangles'] / 100.

    def ToNuistRadar(self):
        """将WSR98D数据转为Nuist Radar的数据格式"""

        return NuistRadar(fields=self.fields, scan_type=self.scan_type, time=self.get_scan_time(), \
                          range=self.range, azimuth=self.azimuth, elevation=self.elevation, latitude=self.latitude, \
                          longitude=self.longitude, altitude=self.altitude,
                          sweep_start_ray_index=self.sweep_start_ray_index, \
                          sweep_end_ray_index=self.sweep_end_ray_index, fixed_angle=self.get_fixed_angle(), \
                          bins_per_sweep=self.bins_per_sweep, nyquist_velocity=self.get_NRadar_nyquist_speed(), \
                          frequency=self.frequency, unambiguous_range=self.get_NRadar_unambiguous_range(), \
                          nrays=self.nrays, nsweeps=self.nsweeps)

    def ToPyartRadar(self):
        pass


if __name__ == "__main__":
    start = time.time()
    test = SCBaseData(r"E:\RadarBaseData\CINRAD_SC_old\Z_RADR_I_Z9280_20180209132700_O_DOR_SC_CAP.bin")
    SC = SC2NRadar(test)
    end = time.time()
    print(end-start)