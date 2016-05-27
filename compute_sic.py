#!/usr/bin/env python
'''
Classify AVHRR GAC data surface types and compute sea ice concentration

Surface classification code written by Steinar Eastwood, FoU

'''

import os
import h5py
import argparse
import datetime

import numpy as np
import numpy.ma as ma
import pyresample as pr
import matplotlib
# matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.cm as cm
import sys
from scipy import stats
from pyresample import kd_tree

import netCDF4
import datetime

from matplotlib import mlab
from scipy import ndimage

def load_extent_mask(filepath):
    '''
    Args:
        filepath (str) : path to filepath
    '''
    data = np.load(filepath)
    extent_mask = data['extent_mask']
    return extent_mask

def solve(m1,m2,std1,std2):
    a = 1/(2*std1**2) - 1/(2*std2**2)
    b = m2/(std2**2) - m1/(std1**2)
    c  = m1**2 /(2*std1**2) - m2**2 / (2*std2**2) - np.log(std2/std1)
    return np.roots([a,b,c])


def clean_up_cloudmask(cloudmask, lats):
    """ Clean up large chunks of errors in cloudmask

    Return updated cloudmask

    There appears to be big blocks of open water in the cloudmask around the pole
    Which is clearly wrong.

    Use scipy.ndimage to find large objects and remove them
    Solution adopted from "http://www.scipy-lectures.org/advanced/image_processing"
    """
    mask = np.where(((cloudmask==1) * (lats>80))==True, True, False)
    labels, nlabels = ndimage.label(mask)
    sizes = ndimage.sum(mask, labels, range(nlabels + 1))
    mask_sizes = sizes > 10
    remove_pixels = mask_sizes[labels]
    updated_cloudmask_data = np.where(remove_pixels==True, 0, cloudmask.data)

    return np.ma.array(updated_cloudmask_data,  mask= cloudmask.mask)


def compute_sic( data, cloudmask, coeffs, coeff_indices, lons, lats, soz ):
    """compute sea ice concentration

    use probability information to select tie points
    pixel with highest probability of sea ice or water
    are selected as dynamic tiepoints

    args:
        data (numpy.ndarray):   observation array for computing sic

    returns:
        sic (numpy.ndarray):    array with sea ice concentration values
    """

    sic_with_water = np.ma.array(np.zeros(cloudmask.shape), mask=cloudmask.mask)
    # pick the pixels where the probability of ice is higher than other surface types

    # don't use pixels that are: clouds contaminated (2)
    #                            clouds filled (3)
    #                            not processed (0)
    #                            undefined (5)
    #                            sun elevation angles above 90 degrees

    # only_ice_mask = (cloudmask == 4) * (soz.mask==False) * (soz < 89)
    # only_ice_data = ma.array(data, mask = ~only_ice_mask)
    # only_water_mask = (cloudmask != 4) * (cloudmask != 2) * (cloudmask !=3)

    ice_mean = np.ma.array(coeffs[coeff_indices][:,:,1], mask = coeffs[coeff_indices][:,:,1]==0)
    ice_mean = np.ma.fix_invalid(ice_mean)
    ice_std = coeffs[coeff_indices][:,:,2]
    ice_std = np.ma.fix_invalid(ice_std)
    ice_std = np.where(ice_std >= ice_mean, ice_mean/3, ice_std) # correct values that stand out too much
    water_threshold = 3 # reflectance of water is roughly 3 percent

    mask = (cloudmask == 2) + (cloudmask == 3) + (cloudmask == 0) + (cloudmask == 5) + (soz > 89) + ( (data>3) * (data<ice_std/2))
    water_mask = (cloudmask == 1) * (data < water_threshold + 2)


    sic = 100*data/(ice_mean - ice_std/2)
    sic = np.where(sic>100, 100, sic)
    sic = np.where(data <= water_threshold, 0, sic)
    sic = np.where(water_mask == True, 0, sic)

    sic = np.ma.array(sic, mask = (cloudmask.mask + mask))

    return sic

def get_osisaf_land_mask(filepath):
    """
    Load a OSI SAF landmask using numpy.load
    args:
        filepath (str) : path to file
    """
    data = np.load(filepath)
    land_mask = data['land_mask'].astype('bool')
    return land_mask

def save_sic(output_filename, sic, timestamp, lon, lat):

    filehandle = netCDF4.Dataset(output_filename, 'w')
    filehandle.createDimension('time', size=1)
    filehandle.createVariable('time', 'l', dimensions=('time'))
    filehandle.variables['time'].units = "seconds since 1970-1-1"
    filehandle.variables['time'][:] = timestamp
    filehandle.createDimension('x', size=sic.shape[0])
    filehandle.createDimension('y', size=sic.shape[1])

    filehandle.createVariable('lon', 'float', dimensions=( 'x', 'y'))
    filehandle.createVariable('lat', 'float', dimensions=('x', 'y'))
    filehandle.variables['lon'].units = 'degrees_east'
    filehandle.variables['lat'].units = 'degrees_north'
    filehandle.variables['lon'][:] = lon
    filehandle.variables['lat'][:] = lat
    filehandle.variables['lon'].missing_value = -32767
    filehandle.variables['lon'].fill_value = -32767
    filehandle.variables['lat'].missing_value = -32767
    filehandle.variables['lat'].fill_value = -32767

    filehandle.createVariable('ice_conc', 'f4', dimensions=('time', 'x', 'y'))
    filehandle.variables['ice_conc'].coordinates = "lon lat"
    filehandle.variables['ice_conc'].units = "%"
    filehandle.variables['ice_conc'].missing_value = -32767
    filehandle.variables['ice_conc'].fill_value = -32767
    filehandle.variables['ice_conc'][:] = sic


    filehandle.close()

def compose_filename(data, sensor_name):
    timestamp = datetime.datetime.fromtimestamp(data.variables['time'][0])
    timestamp_string = timestamp.strftime('%Y%m%d_%H%M')
    filename = '{}_iceconc_{}_arctic.nc'.format(sensor_name,timestamp_string)
    return filename

def apply_mask(mask_array, data_array):
    """
    Apply mask to data array

    Args:
        mask (numpy.ndarray) : boolean array
        data (numpy.ma.ndarray) : numerical masked array

    Returns:
        masked_data_array (numpy.ma.ndarray) : masked array
    """
    # masked_data_array = np.ma.array(data_array.data, mask = data_array.mask + mask_array)

    masked_data_array = np.where(mask_array == True, 200, data_array.data)
    original_mask = data_array.mask

    combined_mask = np.where(mask_array == True, False, original_mask)
    data_array_with_combined_mask = np.ma.array(masked_data_array, mask = combined_mask)

    return data_array_with_combined_mask


def main():

    p = argparse.ArgumentParser()
    p.add_argument("-o", "--output-dir", default='.', nargs=1)
    p.add_argument('-c', '--coeffs', nargs=1,
                         help='Name of the area definition',
                         type=str)
    p.add_argument("-i", "--input-file", nargs=1,
                         help="Input Mitiff Files")
    p.add_argument("-a", "--areas_file", nargs=1,
                         help="Areas definition file")
    p.add_argument('-s', '--sensor', nargs=1,
                         help='Name of the sensor, e.g. avhrr_metop02',
                         type=str)
    p.add_argument('-m', '--mean-coeffs', nargs=1, help='mean and standard deviation over ice')
    p.add_argument('-e', '--extent-mask-file', nargs=1, help='climatological ice extent mask')

    args = p.parse_args()
    areas_filepath = args.areas_file[0]


    # Read in test coefficients file for daytime
    # coeffs_filename = 'coeffPDF_daytime_mean-std-line_v2p1.txt'
    # coeffs_filename = args.coeffs[0] # "./coeffPDF_daytime_mean-std-line_v2p2-misha.txt"
    # coeffs = read_coeffs_from_file(coeffs_filename)
    sensor_name = args.sensor[0]

    mean_coeffs = np.load(args.mean_coeffs[0])
    # reduce coefficients to just the ones needed for this sensor
    # coeffs = coeffs[np.logical_and(coeffs['sensor']==sensor_name, coeffs['datatype']=='gac')]

    # Read in test AVHRR swath file, with lat/lon info (for trimming)
    avhrr_filepath = args.input_file[0]
    avhrr_dirpath = os.path.dirname(avhrr_filepath)
    avhrr_basename = os.path.basename(avhrr_filepath)
    avhrr = netCDF4.Dataset(avhrr_filepath, locations=True)

    # pigobs, pcgobs, pwgobs = calc_wic_prob_day_twi(coeffs, avhrr)

    vis06 = avhrr.variables['vis06'][0,:,:]
    vis09 = avhrr.variables['vis09'][0,:,:]
    lons = avhrr.variables['lon'][0,:,:]
    lats = avhrr.variables['lat'][0,:,:]
    cloudmask = avhrr.variables['cloudmask'][0,:,:]


    soz = avhrr.variables['sunsatangles'][0,:,:]
    SOZ_LOWLIM = 0
    SOZ_HIGHLIM =89
    SOZ = soz.astype(np.int16)
    coeff_indices = np.where((SOZ >= SOZ_LOWLIM) * (SOZ <= SOZ_HIGHLIM), SOZ, 0)

    cloudmask = clean_up_cloudmask(cloudmask, lats)
    sic = compute_sic(vis09, cloudmask, mean_coeffs, coeff_indices, lons, lats, SOZ)

    sic_filename = compose_filename(avhrr, sensor_name)
    output_path = os.path.join(args.output_dir[0], sic_filename)

    extent_mask_file = args.extent_mask_file[0]
    extent_mask = load_extent_mask(extent_mask_file)
    sic = np.ma.array(sic, mask = (sic.mask == True) + (extent_mask == False))

    # Load OSI SAF landmask and apply to resampled SIC array
    land_mask_filepath = os.path.join(os.path.dirname(
                                      os.path.abspath(__file__)),
		                              'resources',
                                      'land_mask.npz')

    land_mask = get_osisaf_land_mask(land_mask_filepath)
    sic = apply_mask(land_mask, sic)


    save_sic(output_path,
                 sic,
                 avhrr.variables['time'][0],
                 avhrr.variables['lon'][:,:],
                 avhrr.variables['lat'][:,:])



def calc_wic_prob_day_twi(coeffs, avhrr):
    ''' Calculate water-ice-cloud daytime and twilight probabilities.'''

    # Use A06 or not
    useA06 = True

    # Defining undef values
    iceclflundef = -1.0
    prob_undef   = -999.0

    # Put data in variables with shorter name, just for simplicity
    A06 = np.ma.array( avhrr.variables['vis06'][0,:,:], mask = avhrr.variables['vis06'][0,:,:] <0 )# avhrr.data[1]
    A09 = np.ma.array(avhrr.variables['vis09'][0,:,:], mask = avhrr.variables['vis09'][0,:,:] < 0)# avhrr.data[2]
    A16 = np.ma.array(avhrr.variables['vis16'][0,:,:], mask = avhrr.variables['vis16'][0,:,:] < 0)
    T37 = np.ma.array(avhrr.variables['tb37'][0,:,:], mask = avhrr.variables['tb37'][0,:,:] < 0)
    T11 = np.ma.array(avhrr.variables['tb11'][0,:,:], mask = avhrr.variables['tb11'][0,:,:] < 0)
    SOZ = avhrr.variables['sunsatangles'][0,:,:]
    SOZ_LOWLIM = 0
    SOZ_HIGHLIM = 89

    # Turn the SOZ numbers into ints suitable for indexing (truncate float to int)
    SOZ = SOZ.astype(np.int16)
    coeff_indices = np.where((SOZ >= SOZ_LOWLIM) * (SOZ <= SOZ_HIGHLIM), SOZ, 0)

    # Decide which data to use.
    # Especially important for chosing between re1.6/re0.6 and bt3.7-bt11. Prefer to use 1.6 if available.
    useA0906  = (A06 >= 0.00001) * (A09 >= 0.00001)
    useA0906 *= (A06 <= 100.0) * (A09 <= 100.0)
    useA0906 *= (SOZ > 0.0) * (SOZ < SOZ_HIGHLIM)
    useA16    = (A16 > 0.00001) * (A16 <= 100.0)
    useA16   *= (SOZ > SOZ_LOWLIM) * (SOZ < SOZ_HIGHLIM)
    useT37    = np.invert(useA16) * (T37 > 50.0)
    useT37   *= (T37 < 400.0) * (T11 > 50.0)
    useT37   *= (T11 < 400.0) * (SOZ > SOZ_LOWLIM) * (SOZ < SOZ_HIGHLIM)

    # Combine the input variables to the features to be used
    A0906 = (A09 / A06)
    try:
        A1606 = (A16 / A06)
    except:
        pass

    # T3711 = (T37-T11)
    # Constants
    Lsun = 5.112
    Aval = 0 # 1.592459
    Bval = 1 # 0.998147
    Vc = 2674.81  # 1 # 2700.1148

    C1 = 1.1910427 * 0.00001
    C2 = 1.438775

    # compute effective temperature
    Tch37 = Aval+ Bval * T37
    Tch11 = Aval + Bval * T11

    # Compute radiance of 3.7 and 11 microns channels
    Ne37 = ( C1 * ( Vc ** 3) )/( np.exp(C2 * Vc/Tch37) -1.)
    Ne11 = ( C1 * ( Vc ** 3) )/( np.exp(C2 * Vc/Tch11) -1.)

    #Calculate distance to Sun
    doy = 213
    theta0 = (2. * np.pi * doy - 1 ) / 365.
    dcorr = ( 1.000110 + 0.034221 * np.cos(theta0) + 0.001280*np.sin(theta0) + 0.000719*np.cos(2.*theta0) + 0.000077*np.sin(2.*theta0)) # distance correction acc. to DOY
    sollum = dcorr * Lsun * np.cos(np.deg2rad(SOZ))

    # Substract 11 microns channel from T37 to calculate 3.7 reflectance
    A37nonmasked = 100 * ( (Ne37 - Ne11) / (sollum - Ne11))
    A37 = np.ma.array(A37nonmasked, mask = (A37nonmasked > 100) + (A37nonmasked < 0))
    A3706 = np.ma.array(A37 / A06, mask = (A37/A06<0) + (A37/A06>1))
    print 'A37', A37.mean(), A37.max(), A37.min()
    print 'A06', A06.mean(), A06.max(), A06.min()
    print 'A3706', A3706.mean(), A3706.max(), A3706.min()


    # Estimate the probability of getting the observed A09/A06 and A16/A06 or T37-T11
    # given ice, cloud or water.

    # First, always calculate the re0.9/re0.6 and re0.6 probabilities and put in VAR1 and VAR2 variables
    var = 're09/re06'
    cloud_mean, cloud_std, sea_mean, sea_std, ice_mean, ice_std = get_coeffs_for_var(coeffs, var,
                                                                indices=coeff_indices)

    pVAR1gc = normalpdf(A0906, cloud_mean, cloud_std)
    pVAR1gw = normalpdf(A0906, sea_mean, sea_std)
    pVAR1gi = normalpdf(A0906, ice_mean, ice_std)

    if (useA06):
        var = 're06'
        cloud_mean, cloud_std, sea_mean, sea_std, ice_mean, ice_std = get_coeffs_for_var(coeffs, var,
                                                                                         indices=coeff_indices)
        ice_mean_ma = np.ma.array(cloud_mean, mask = ((SOZ.mask==True)+(SOZ.data > 70)))
        water_mean_ma = np.ma.array(sea_mean, mask = ((SOZ.mask==True)+(SOZ.data > 70)))
        soz_ma = np.ma.array(SOZ, mask = ((SOZ.mask==True)+(SOZ.data > 70)))
        # plt.clf();plt.imshow(np.ma.array(water_mean_ma)); plt.colorbar();plt.savefig('water_mean.png')
        # plt.clf();plt.imshow(np.ma.array(ice_mean, mask = ((SOZ.mask==True)+(SOZ.data > 70)))); plt.colorbar();plt.savefig('ice_mean.png')
        # plt.clf();plt.imshow(np.ma.array(SOZ, mask = ((SOZ.mask==True)+(SOZ.data > 70)))); plt.colorbar();plt.savefig('sozn.png')
        # plt.clf(); plt.plot(soz_ma.compressed(), ice_mean_ma.compressed());plt.savefig('soz-ice-plot.png')
        # plt.clf(); plt.plot(soz_ma.compressed(), water_mean_ma.compressed());plt.savefig('soz-sea-plot.png')


        # slope, intercept, r_value, p_value, std_err = stats.linregress(soz_ma.compressed(), ice_mean_ma.compressed())
        # print slope, intercept, std_err

        pVAR2gc = normalpdf(A06, cloud_mean, cloud_std)
        pVAR2gw = normalpdf(A06, sea_mean, sea_std)
        pVAR2gi = normalpdf(A06, ice_mean, ice_std)


    # Calculate the re1.6/re0.6 probabilities if any of the input data have the 1.6um channel
    if (useA16.any()):
        var = 're16/re06'
        cloud_mean, cloud_std, sea_mean, sea_std, ice_mean, ice_std = get_coeffs_for_var(coeffs, var,
                                                                        indices=coeff_indices)

        pA1606gc = normalpdf(A1606, cloud_mean, cloud_std)
        pA1606gw = normalpdf(A1606, sea_mean, sea_std)
        pA1606gi = normalpdf(A1606, ice_mean, ice_std)

    # Calculate the bt3.7-bt11 probabilities if any of the input data have the 3.7um channel
    if (useT37.any()):
        # var = 'bt37-bt11'
        var = 're37/re06'
        cloud_mean, cloud_std, sea_mean, sea_std, ice_mean, ice_std = get_coeffs_for_var(coeffs, var,
                                                                            indices=coeff_indices)

        pT3711gc = normalpdf(A3706, ice_mean, ice_std)
        pT3711gw = normalpdf(A3706, cloud_mean, cloud_std)
        pT3711gi = normalpdf(A3706, sea_mean, sea_std)

    # Put the re1.6/re0.6 based or bt3.7-bt11 based probabilites in VAR2 variables
    # re1.6/re0.6 have first priority. First fill with bt3.7-bt11, then overwrite
    # with re1.6/re0.6
    anyVAR3 = False
    if (useT37.any()):
        anyVAR3 = True
        pVAR3gw = pT3711gw.copy()
        pVAR3gi = pT3711gi.copy()
        pVAR3gc = pT3711gc.copy()
        if (useA16.any()):
            # TODO: check... only replacing values where there is an A16 value
            pVAR3gw[useA16] = pA1606gw[useA16]
            pVAR3gi[useA16] = pA1606gi[useA16]
            pVAR3gc[useA16] = pA1606gc[useA16]
    elif (useA16.any()):
        anyVAR3 = True
        pVAR3gw = pA1606gw.copy()
        pVAR3gi = pA1606gi.copy()
        pVAR3gc = pA1606gc.copy()

    useVAR3  = useA16 + useT37


    # Use Bayes theorem and estimate probability for ice, water and cloud.
    # Assumes equal apriori probability for ice, water, and cloud.

    # First, calculate only using VAR1 and VAR2
    if (useA06) :
        psumVAR12 = (pVAR1gi*pVAR2gi) + (pVAR1gw*pVAR2gw) + (pVAR1gc*pVAR2gc)
        pigobs = ((pVAR1gi*pVAR2gi) / psumVAR12)
        pwgobs = ((pVAR1gw*pVAR2gw) / psumVAR12)
        pcgobs = ((pVAR1gc*pVAR2gc) / psumVAR12)
    else:
        psumVAR1 = pVAR1gi + pVAR1gw + pVAR1gc
        pigobs  = (pVAR1gi/psumVAR1)
        pwgobs  = (pVAR1gw/psumVAR1)
        pcgobs  = (pVAR1gc/psumVAR1)

    # Then, calculate using both VAR1, VAR2 and VAR3, and overwrite the results from
    # using only VAR1 where there are valid VAR2 data.
    if (anyVAR3):
        if (useA06):
            psumVAR123 = (pVAR1gi*pVAR2gi*pVAR3gi) + (pVAR1gw*pVAR2gw*pVAR3gw) + (pVAR1gc*pVAR2gc*pVAR3gc)
            pigobs[useVAR3] = ((pVAR1gi[useVAR3]*pVAR2gi[useVAR3]*pVAR3gi[useVAR3]) / psumVAR123[useVAR3])
            pwgobs[useVAR3] = ((pVAR1gw[useVAR3]*pVAR2gw[useVAR3]*pVAR3gw[useVAR3]) / psumVAR123[useVAR3])
            pcgobs[useVAR3] = ((pVAR1gc[useVAR3]*pVAR2gc[useVAR3]*pVAR3gc[useVAR3]) / psumVAR123[useVAR3])
        else:
            psumVAR123 = (pVAR1gi*pVAR3gi) + (pVAR1gw*pVAR3gw) + (pVAR1gc*pVAR3gc)
            pigobs[useVAR3] = ((pVAR1gi[useVAR3]*pVAR3gi[useVAR3]) / psumVAR123[useVAR3])
            pwgobs[useVAR3] = ((pVAR1gw[useVAR3]*pVAR3gw[useVAR3]) / psumVAR123[useVAR3])
            pcgobs[useVAR3] = ((pVAR1gc[useVAR3]*pVAR3gc[useVAR3]) / psumVAR123[useVAR3])


    # Quality check on the probabilities
    falsevalue = (pwgobs > 1.0)*(pwgobs < 0.0)
    falsevalue *= (pigobs > 1.0)*(pigobs < 0.0)
    falsevalue *= (pcgobs > 1.0)*(pcgobs < 0.0)
    falsevalue *= ma.is_masked(A06)

    pigobs[falsevalue] = prob_undef
    pcgobs[falsevalue] = prob_undef
    pwgobs[falsevalue] = prob_undef

    return (pigobs,pcgobs,pwgobs)

def get_coeffs_for_var(coeffs, var, indices=None):
    ''' Return mean and std arrays for given variable.'''
    a = coeffs[coeffs['var']==var]['coeffs']
    cloud_coeffs, sea_coeffs, ice_coeffs, snow_coeffs, land_coeffs= coeffs[coeffs['var']==var]['coeffs']

    # Get array of coefficients indexed by integer SOZ, split into mean and std
    # TODO: this is gross
    cloud_mean, cloud_std = cloud_coeffs[indices][:,:,1], cloud_coeffs[indices][:,:,2]
    sea_mean, sea_std = sea_coeffs[indices][:,:,1], sea_coeffs[indices][:,:,2],
    ice_mean, ice_std = ice_coeffs[indices][:,:,1], ice_coeffs[indices][:,:,2]


    return cloud_mean, cloud_std, sea_mean, sea_std, ice_mean, ice_std

def read_coeffs_from_file(filename):
    ''' Read coeffs from file and return numpy rec array with all values.

    Values of array:
        'var': variable
        'sensor': avhrr.11
        'wic': [water|ice|cloud]
        'coeffs': (91,2) array of (mean, std)
     '''
    dtype = [('sensor', 'a15'),
             ('datatype','a6'),
             ('SOT','a9'),
            ('var', 'a18'),
            ('wic', 'a8'),
             ('FCD', 'a8'),
            ('coeffs', 'f4', (91,3))
            #('coeffs',
            #   [('mean', 'f4'),
            #   ('std', 'f4')], 91)
            ]
    coeffs = np.genfromtxt(filename, dtype=dtype)

    return coeffs

def normalpdf(x, mu, sigma):
    ''' Calculate Gaussian distribution.

        mu: mean of distribution,
        sigma: std of distribution,
        x: value for which to calculate
    '''

    gpdf = np.zeros(len(x))
    gpdf = (1.0/(((2.0*np.pi)**0.5)*sigma)) * np.exp(-1.0*((x-mu)**2.0) / (2.0* (sigma**2.0)))
    return(gpdf)

def lognormalpdf(x, mu, sigma):
    ''' Calculate log-normal distribution.

    mu: mean of log of distribution,
    sigma: std of log of distribution,
    x: value for which to calculate
    '''

    gpdf = np.zeros(len(x))
    z = np.power((np.log(x)-mu),2.0)/(sigma**2.0)
    e = math.e**(-0.5*z**2.0)
    C = x*sigma*math.sqrt(2.0*math.pi)
    gpdf = 1.0*e/C
    return(gpdf)


if __name__ == '__main__':
    main()
