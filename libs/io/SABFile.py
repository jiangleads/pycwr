# -*- coding: utf-8 -*-
import struct
import numpy as np
from scipy import interpolate
from BaseDataProtocol.SABProtocol import dtype_sab
from util import _prepare_for_read, _unpack_from_buf, julian2date, get_radar_info
import time
from libs.core.NRadar import NuistRadar

class SABBaseData(object):
    """
    解码SA/SB/CB/SC2.0的雷达数据，仅仅对数据（dBZ, V, W）做了转换
    """

    def __init__(self, filename):
        super(SABBaseData, self).__init__()
        self.filename = filename
        self.fid = _prepare_for_read(self.filename)
        self.RadialNum, self.nrays = self._RadialNum_SAB_CB() ##检查文件有无问题
        self.radial = self._parse_radial()
        status = np.array([istatus['RadialStatus'] for istatus in self.radial[:]])
        self.sweep_start_ray_index = np.where((status==0)|(status==3))[0]
        self.sweep_end_ray_index = np.where((status == 2) | (status == 4))[0]
        self.nsweeps = len(self.sweep_start_ray_index)
        self.fid.close()

    def _RadialNum_SAB_CB(self):
        """f: a file-like object was provided, 确定雷达数据的径向字节长度"""
        assert self.fid.read(28)[14:16] == b'\x01\x00', 'file in not a valid SA/SB/CB file!'
        self.fid.seek(0, 0) ##移动到开头
        data_len = len(self.fid.read())
        assert (data_len%2432 == 0) | (data_len%4132 == 0) |(data_len%3132 == 0), "file size has problems!"
        ###判断雷达数据类型SA/SB 或者 CB
        if data_len % 2432 == 0:
            RadialNum = 2432
            self.Type = "SAB"
        elif data_len%4132 == 0:
            RadialNum = 4132
            self.Type = 'CB'
        else:
            RadialNum = 3132
            self.Type = 'SC'
        self.fid.seek(0, 0) ##移动到开头
        return RadialNum, int(data_len/RadialNum)

    def _parse_radial(self):
        """
        循环读取所有径向数据
        :param fid:
        :return:
        """
        radial = []
        for _ in range(self.nrays):
            radial.append(self._parse_radial_single(self.fid.read(self.RadialNum)))
        return radial

    def _parse_radial_single(self, radial_buf):
        Radial = {}
        RadialHeader, size_tmp = _unpack_from_buf(radial_buf, 0, dtype_sab.RadialHeader())
        Radial.update(RadialHeader)
        RadialDataDtype = dtype_sab.RadialData(RadialHeader['GatesNumberOfReflectivity'],
                                               RadialHeader['GatesNumberOfDoppler'])
        FieldSize = RadialHeader['GatesNumberOfReflectivity'] + RadialHeader['GatesNumberOfDoppler']*2
        RadialData = np.frombuffer(radial_buf[size_tmp:size_tmp+FieldSize], dtype=RadialDataDtype)
        Radial['fields'] = {}
        Radial['fields']['dBZ'] = np.where(RadialData['dBZ']>1, (RadialData['dBZ'].astype(int) - 2)/2.-32,
                                           np.nan).astype(np.float32)
        Radial['fields']['V'] = np.where(RadialData['V'] > 1, (RadialData['V'].astype(int) - 2) / 2. - 63.5,
                                           np.nan).astype(np.float32)
        Radial['fields']['W'] = np.where(RadialData['W'] > 1, (RadialData['W'].astype(int) - 2) / 2. - 63.5,
                                         np.nan).astype(np.float32)
        return Radial

    def get_nyquist_velocity(self):
        """get nyquist vel per ray
        获取每根径向的不模糊速度
        :return:(nRays)
        """
        return np.array([iradial['Nyquist']/100. for iradial in self.radial])
    def get_unambiguous_range(self):
        """
        获取每根径向的不模糊距离 units:km
        :return:(nRays)
        """
        return np.array([iradial['URange']/10. for iradial in self.radial])
    def get_scan_time(self):
        """
        获取每根径向的扫描时间
        :return:(nRays)
        """
        return np.array([julian2date(iradial['JulianDate'], iradial['mSends']) for iradial in self.radial])
    def get_sweep_end_ray_index(self):
        """
        获取每个sweep的结束的index，包含在内
        :return:(nsweep)
        """
        return self.sweep_end_ray_index
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
        return self.sweep_end_ray_index - self.sweep_start_ray_index + 1

    def get_azimuth(self):
        """
        获取每根径向的方位角
        :return:(nRays)
        """
        return np.array([iradial['AZ']/8.* 180./4096. for iradial in self.radial])
    def get_elevation(self):
        """
        获取每根径向的仰角
        :return: (nRays)
        """
        return np.array([iradial['El']/8.* 180./4096. for iradial in self.radial])

    def get_latitude_longitude_altitude_frequency(self):
        """
        获取经纬度高度，雷达频率
        :return:lat, lon, alt, frequency
        """
        return get_radar_info(self.filename)

    def get_scan_type(self):
        """
        获取扫描的类型
        :return:
        """
        return "ppi"

class SAB2NRadar(object):
    """到NusitRadar object 的桥梁"""
    def __init__(self, SAB):
        self.SAB = SAB
        v_index_alone = self.get_v_idx()
        dBZ_index_alone = self.get_dbz_idx()
        for index_with_dbz, index_with_v in zip(dBZ_index_alone, v_index_alone):
            assert abs(self.SAB.get_elevation()[index_with_v] - \
                    self.SAB.get_elevation()[index_with_dbz]) < 0.5,"warning! maybe it is a problem."
            self.interp_dBZ(index_with_dbz, index_with_v)
        ind_remove = self.get_reomve_radial_num()
        self.radial = [iray for ind, iray in enumerate(self.SAB.radial) if ind not in ind_remove]
        self.nrays = len(self.radial)
        self.nsweeps = self.SAB.nsweeps - dBZ_index_alone.size
        status = np.array([istatus['RadialStatus'] for istatus in self.radial[:]])
        self.sweep_start_ray_index = np.where((status == 0) | (status == 3))[0]
        self.sweep_end_ray_index = np.where((status == 2) | (status == 4))[0]
        self.scan_type = self.SAB.get_scan_type()
        self.latitude, self.longitude, self.altitude, self.frequency = \
            self.SAB.get_latitude_longitude_altitude_frequency()
        self.bins_per_sweep = self.get_nbins_per_sweep()
        self.max_bins = self.bins_per_sweep.max()
        self.range = self.get_range_per_radial(self.max_bins) ##所有的数据向多普勒数据对齐
        self.azimuth = self.get_azimuth()
        self.elevation = self.get_elevation()
        self.fields = self._get_fields()

    def get_reomve_radial_num(self):
        """获得需要remove的radial的index"""
        """获得需要remove的radial的index"""
        dBZ_alone = self.get_dbz_idx()
        index_romove = []
        for isweep in dBZ_alone:
            index_romove.extend(range(self.SAB.sweep_start_ray_index[isweep], \
                                      self.SAB.sweep_end_ray_index[isweep] + 1))
        return index_romove

    def get_v_idx(self):
        """获取需要插值的sweep, 插值到有径向速度仰角"""
        flag = np.array([((self.SAB.radial[idx]['fields']["V"].size != 0) and
                        (self.SAB.radial[idx]['fields']["dBZ"].size == 0)) \
                         for idx in self.SAB.sweep_start_ray_index])
        return np.where(flag == 1)[0]
    def get_dbz_idx(self):
        """获取含有dbz的sweep"""
        flag = np.array([((self.SAB.radial[idx]['fields']["V"].size == 0) and
                          (self.SAB.radial[idx]['fields']["dBZ"].size != 0)) \
                         for idx in self.SAB.sweep_start_ray_index])
        return np.where(flag == 1)[0]

    def interp_dBZ(self, field_with_dBZ_num, field_without_dBZ_num):
        """
        将dBZ插值到不含dBZ的仰角
        :param field_with_dBZ_num: 要插值的sweep num, （从0开始）
        :param field_without_dBZ_num: 要插值到的sweep num, (从0开始)  which to evaluate the interpolated values
        :return:
        """
        azimuth = self.SAB.get_azimuth() ##
        assert (field_with_dBZ_num + 1) == field_without_dBZ_num, "check interp sweep!"
        dbz_az = azimuth[self.SAB.sweep_start_ray_index[field_with_dBZ_num]: \
                         self.SAB.sweep_end_ray_index[field_with_dBZ_num] + 1]
        v_az = azimuth[self.SAB.sweep_start_ray_index[field_without_dBZ_num]: \
                       self.SAB.sweep_end_ray_index[field_without_dBZ_num] + 1]
        dbz_idx = np.argmin(np.abs(dbz_az.reshape(-1, 1) - v_az.reshape(1, -1)), axis=0) + \
                  self.SAB.sweep_start_ray_index[field_with_dBZ_num]
        v_idx = np.arange(self.SAB.sweep_start_ray_index[field_without_dBZ_num], \
                          self.SAB.sweep_end_ray_index[field_without_dBZ_num] + 1)
        for ind_dbz, ind_v in zip(dbz_idx, v_idx):
            self.SAB.radial[ind_v]["fields"]['dBZ'] = self.SAB.radial[ind_dbz]["fields"]['dBZ']


    def get_azimuth(self):
        """
        获取每根径向的方位角
        :return:(nRays)
        """
        return np.array([iradial['AZ'] / 8. * 180. / 4096. for iradial in self.radial])

    def get_elevation(self):
        """
        获取每根径向的仰角
        :return: (nRays)
        """
        return np.array([iradial['El']/8.* 180./4096. for iradial in self.radial])

    def get_rays_per_sweep(self):
        """
        获取每个sweep的径向数
        :return:(nsweep)
        """
        return self.sweep_end_ray_index - self.sweep_start_ray_index + 1

    def get_scan_time(self):
        """
        获取每根径向的扫描时间
        :return:(nRays)
        """
        return np.array([julian2date(iradial['JulianDate'], iradial['mSends']) for iradial in self.radial])

    def get_nyquist_velocity(self):
        """get nyquist vel per ray
        获取每根径向的不模糊速度
        :return:(nRays)
        """
        return np.array([iradial['Nyquist']/100. for iradial in self.radial])

    def get_unambiguous_range(self):
        """
        获取每根径向的不模糊距离
        :return:(nRays)
        """
        return np.array([iradial['URange']/10. for iradial in self.radial])

    def get_sweep_end_ray_index(self):
        """
        获取每个sweep的结束的index，包含在内
        :return:(nsweep)
        """
        return self.sweep_end_ray_index
    def get_sweep_start_ray_index(self):
        """
        获取每个sweep的开始的index
        :return:(nsweep)
        """
        return self.sweep_start_ray_index

    def get_nbins_per_sweep(self):
        """
        确定每个sweep V探测的库数
        :return:
        """
        return np.array([self.radial[idx]['fields']['V'].size for idx in self.sweep_start_ray_index])

    def get_range_per_radial(self, length):
        """
        确定径向每个库的距离
        :param length:
        :return:
        """
        Resolution = self.radial[0]["GateSizeOfDoppler"]
        return np.linspace(Resolution, Resolution * length, length)

    def get_dbz_range_per_radial(self, length):
        """
        确定径向每个库的距离
        :param length:
        :return:
        """
        Resolution = self.radial[0]["GateSizeOfReflectivity"]
        start_range = self.radial[0]["GateSizeOfDoppler"]
        return np.linspace(start_range, start_range + Resolution * (length - 1), length)

    def _get_fields(self):
        """将所有的field的数据提取出来"""
        fields = {}
        field_keys = self.radial[0]['fields'].keys()
        for ikey in field_keys:
            fields[ikey] = np.array([self._add_or_del_field(iray['fields'], ikey) for iray in self.radial])
        return fields

    def _add_or_del_field(self, dat_fields, key):
        """
        根据fields的key提取数据, 将dbz的数据和dop的数据分辨率统一
        :param dat_fields: fields的数据
        :param key: key words
        :return:
        """
        length = self.max_bins
        if key == "dBZ":
            dbz_range = self.get_dbz_range_per_radial(dat_fields[key].size)
            dop_range = self.range
            match_data = interpolate.interp1d(dbz_range, dat_fields[key], kind="nearest",
                                              bounds_error=False, fill_value=np.nan)
            dat_ray = match_data(dop_range)
            return dat_ray.ravel()
        else:
            dat_ray = dat_fields[key]
        if dat_ray.size >= length:
            return (dat_ray[:length]).ravel()
        else:
            out = np.full((length,), np.nan)
            out[:dat_ray.size] = dat_ray
            return out.ravel()

    def get_NRadar_nyquist_speed(self):
        """array shape (nsweeps)"""
        return np.array([self.radial[idx]['Nyquist']/100. for idx in self.sweep_start_ray_index])

    def get_NRadar_unambiguous_range(self):
        """array shape (nsweeps)"""
        return np.array([self.radial[idx]['URange']/10. for idx in self.sweep_start_ray_index])

    def get_fixed_angle(self):
        if self.nsweeps == 9:
            fixed_angle = np.array([0.50, 1.45, 2.40, 3.35, 4.30, 6.00, 9.00, 14.6, 19.5])
        elif self.nsweeps == 14:
            fixed_angle = np.array([0.50, 1.45, 2.40, 3.35, 4.30, 5.25, 6.2, 7.5, 8.7, 10, 12, 14, 16.7, 19.5])
        elif self.nsweeps == 6:
            fixed_angle = np.array([0.50, 1.50, 2.50, 2.50, 3.50, 4.50])
        elif self.nsweeps == 4:
            fixed_angle = np.array([0.50, 2.50, 3.50, 4.50])
        else:
            fixed_angle = np.array([self.radial[idx]['El']/8.* 180./4096. for idx in self.sweep_start_ray_index])
        return fixed_angle

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
        """转化为Pyart Radar的对象"""
        pass


if __name__ == "__main__":
    start = time.time()
    #test = SABBaseData("/home/zy/data/code_data/ERIC/Radar/2010081202.13A")
    test = SABBaseData(r"E:\RadarBaseData\CINRAD-SA\z9250\BASE150427\Z_RADR_I_Z9250_20150427111500_O_DOR_SA_CAP.bin")
    SAB = SAB2NRadar(test)
    end = time.time()
    print(end-start)