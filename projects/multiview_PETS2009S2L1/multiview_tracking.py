import sys
import os
import numpy as np
import itertools
from itertools import repeat
import multiprocessing
import threading
import time
from datetime import datetime
import uuid
import json
import imageio
import yaml
import argparse
from shapely.geometry import Polygon, Point

import mot3d
from mot3d.utils import utils
from mot3d.utils import trajectory as utils_traj
from mot3d.weight_functions import (weight_distance_detections_3d, weight_confidence_detections_3d,\
                                    weight_distance_tracklets_3d, weight_confidence_tracklets_3d)   

from tracking_routines import *
import feeders
from scene import Scene

class MultiviewMatcher(object):
    
    def __init__(self, views, calibration, config):
        self.views = views
        self.calibration = calibration        
        self.__c__ = config
        
    def compute(self, detections_2d):
        
        pool = multiprocessing.Pool(self.__c__['threads'])
        detections_3d = pool.starmap(multiview_matching_parallel, zip(detections_2d, 
                                                                      repeat(self.views),
                                                                      repeat(self.calibration),
                                                                      repeat(self.__c__['max_distance']),
                                                                      repeat(self.__c__['distance_none']),
                                                                      repeat(self.__c__['n_candidates']),
                                                                      repeat(self.__c__['n_min_views_clique']),
                                                                      repeat(self.__c__['verbose'])))
        pool.close()
        pool.join() 
        
        return detections_3d

class MultiviewTracker(object):
    
    def __init__(self, scene, config):
        
        self.scene = scene
        self.__c__ = config
        self.batch_size = self.__c__['batch_size']

        __cfgd__ = self.__c__['detections']
        self.__Detection__ = mot3d.Detection3D
        
        weight_distance_d = lambda d1, d2 : weight_distance_detections_3d(d1, d2, **__cfgd__['weight_distance'])
        weight_confidence_d = lambda d : weight_confidence_detections_3d(d, **__cfgd__['weight_confidence'])        
        self.__compute_trajs__ = lambda detections: compute_trajectories(detections, 
                                                                         weight_source_sink=__cfgd__['weight_source_sink'], 
                                                                         weight_distance=weight_distance_d, 
                                                                         weight_confidence=weight_confidence_d,
                                                                         max_jump=__cfgd__['max_jump'],
                                                                         verbose=__cfgd__['verbose'])

        __cfgt__ = self.__c__['tracklets']
        self.__DetectionTracklet__ = lambda id,track,**kwargs: mot3d.DetectionTracklet3D(track, id=id, **kwargs, 
                                                                                         window=__cfgt__['length_endpoints'])

        weight_distance_t = lambda t1,t2 : weight_distance_tracklets_3d(t1, t2, **__cfgt__['weight_distance'])
        weight_confidence_t = lambda t : weight_confidence_tracklets_3d(t, **__cfgt__['weight_confidence'])         
        self.__cat_tracklets__ = lambda tracklets: concatenate_tracklets(tracklets,
                                                                         weight_source_sink=__cfgt__['weight_source_sink'], 
                                                                         weight_distance_tracklets=weight_distance_t,
                                                                         weight_confidence_tracklets=weight_confidence_t,
                                                                         max_jump=__cfgt__['max_jump'],
                                                                         verbose=__cfgt__['verbose'])
        
        self.access_points = self.AccessPoints(self.__c__['access_points'])
        
    class AccessPoints(object):

        def __init__(self, filename):
            # this polygon is the region where N camera's fov overlap
            data = utils.json_read(filename)
            self.p = Polygon(data['contour'])

        def check(self, points):
            return [not self.p.contains(Point(*p[:2])) for p in points]     
        
    def update(self, time_index, detections_3d):
        
        # --------------------------------
        # compute tracklets for this batch
        # --------------------------------
        
        print("[Tracker] Compute tracklets..")        
        tracklets = self.__compute_trajs__(detections_3d)
        
        tracklet_splits = []
        for tracklet in tracklets:
            tracklet_splits += utils_traj.split_trajectory_modulo(tracklet, length=3)
            
        tracklet_splits = utils_traj.remove_short_trajectories(tracklet_splits, th_length=2)

        # --------------------------------------------------------------
        # concatenate the tracklets to the currently active trajectories
        # --------------------------------------------------------------
        
        print("[Tracker] Concatenate tracklets..")
        detections_tracklets =  [self.__DetectionTracklet__(id, track) for id, track in self.scene.active.items()]
        n_skipped = 0
        for tracklet in tracklet_splits:
            #                                     tail                  head
            res = self.access_points.check([tracklet[0].position, tracklet[-1].position])
            if np.all(res):
                # skip tracklets whose endpoints are boths in the access region
                n_skipped += 1
                pass
            else:            
                det_tracklet = self.__DetectionTracklet__(None, tracklet,
                                                          tail_access_point=res[0], 
                                                          head_access_point=res[1])
                detections_tracklets.append(det_tracklet)
                self.scene.store_tracklet(tracklet)
                
        print("{} out of {} tracklets have been discarded.".format(n_skipped, len(tracklet_splits)))

        det_trajectories = self.__cat_tracklets__(detections_tracklets)

        for det_trajectory in det_trajectories:
            # if the id is not None it means that we have added new chunk(s) to one of the existing trajectories
            id = det_trajectory[0].id
            trajectory = utils_traj.concat_tracklets([dt.tracklet for dt in det_trajectory])
            trajectory = utils_traj.interpolate_trajectory(trajectory, attr_names=['position'])
            
            self.scene.update(id, trajectory)  
            
        self.scene.update_completed(time_index)

def main(config_file="main.config"):
    
    # ---------------------------------
    # Initialization
    # ---------------------------------
    __c__ = utils.yaml_read(config_file)
    
    output_path = __c__['output_path']
    utils.mkdir(output_path)
    
    views = __c__['views']
    
    feeder_name = __c__['feeder']['name']
    object_class = __c__['feeder'][feeder_name]['object_class']
    feeder = {view:feeders.Feeder[feeder_name](view, **__c__['feeder'][feeder_name]) for view in views}
    iterator = {view:iter(feeder[view]) for view in views}
    
    calibration = utils.json_read(__c__['calibration'])
    
    scene = Scene(__c__['tracking']['completed_after'])

    matcher = MultiviewMatcher(views, calibration, __c__['matching'])
    tracker = MultiviewTracker(scene, __c__['tracking'])

    start_time_all = time.time()
    
    # ---------------------------------
    # Looping over the data
    # ---------------------------------
    done = False
    while not done:
        
        start_time = time.time()
        
        # ---------------------------------
        # Acquire batch
        # ---------------------------------        
        indexes = []
        detections = []
        for _ in range(tracker.batch_size):
            try:
                _idxs,_detections = list(zip(*[next(iterator[view]) for view in views]))
                indexes.append(_idxs[0])
                detections.append({view:d for view,d in zip(views,_detections)})
            except StopIteration:
                done = True   
                break
        elapsed_time = time.time()-start_time
        print("Data collection time: {:0.3f}s".format(elapsed_time))
            
        # ---------------------------------
        # Compute trackle and connect to the previous ones
        # ---------------------------------             
        if len(detections):
            start_time = time.time()
            detections_3d = matcher.compute(detections) 
            elapsed_time = time.time()-start_time
            print("Matching + triangulation time: {:0.3f}s".format(elapsed_time))
            
            start_time = time.time()
            tracker.update(iterator[views[0]].idx, detections_3d) 
            elapsed_time = time.time()-start_time
            print("Tracking time: {:0.3f}s".format(elapsed_time))            
            
            print("---Summary---")
            print("batch size: {}".format(len(detections)))
            print("indexes: {} -> {}".format(indexes[0], indexes[-1]))
            print("n_active:{} n_completed:{}".format(len(scene.active), len(scene.completed)))
            print("-------------")

    print("Overall time: {:0.2f}s".format(time.time()-start_time_all))        

    output_file = os.path.join(output_path, "results_{}.pickle".format(os.path.splitext(os.path.basename(config_file))[0]))
    scene.save(output_file)

if __name__ == "__main__":

    def str2bool(v):
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Boolean value expected.')    

    parser = argparse.ArgumentParser()   
    parser.add_argument("--config_file", "-c", type=str, default="config.config")

    args = parser.parse_args()

    main(**vars(args))

# python multiview_tracking.py -c config_multiview_PETS2009S2L1.yaml 