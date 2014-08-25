import rospy

import smach
import smach_ros

from scitos_msgs.srv import ResetMotorStop
from scitos_msgs.srv import EnableMotors
from std_srvs.srv import Empty
from scitos_msgs.msg import MotorStatus
from geometry_msgs.msg import Twist

from move_base_msgs.msg import *
import dynamic_reconfigure.client
from scitos_ptu.msg import *
from previous_positions_service.srv import PreviousPosition
from republish_pointcloud_service.srv import RepublishPointcloud
from actionlib_msgs.msg import *

import actionlib

from strands_navigation_msgs.srv import AskHelp, AskHelpRequest


import monitored_navigation.mongo_logger
from strands_navigation_msgs.msg import MonitoredNavEvent
from geometry_msgs.msg import Pose
from actionlib_msgs.msg import GoalID, GoalStatusArray

from scitos_2d_navigation.msg import *

# this file has the recovery states that will be used when some failures are
# detected. There is a recovery behaviour for move_base and another for when the
# bumper is pressed

class RecoverNavReobserveObstacle(smach.State):
    def __init__(self):
        smach.State.__init__(self,
                             outcomes=['succeeded', 'failure', 'preempted'],
                             input_keys=['goal','n_nav_fails'],
                             output_keys=['goal','n_nav_fails'],
                             )

        self.reobserve_client = actionlib.SimpleActionClient('/static_planner', UnleashStaticPlannerAction)
        self.OBSERVATION_TRIES = 3 #will turn into parameter later
                                                  
    def execute(self, userdata):
        print "Failures: ", userdata.n_nav_fails
        if userdata.n_nav_fails > self.OBSERVATION_TRIES:
            return 'failure'
        
        reobserve_goal = UnleashStaticPlannerGoal();
        reobserve_goal.execute = True;
        self.backtrack_client.send_goal(reobserve_goal)
        status = self.reobserve_client.get_state()
        while status == GoalStatus.PENDING or status == GoalStatus.ACTIVE:
            status = self.reobserve_client.get_state()
            if self.preempt_requested():
                self.reobserve_client.cancel_goal()
                self.service_preempt()
                return 'preempted'
            self.reobserve_client.wait_for_result(rospy.Duration(0.2))
        if status == GoalStatus.SUCCEEDED:
            return 'succeeded'
        return 'failure'
    
    def service_preempt(self):
        #check if preemption is working
        smach.State.service_preempt(self)


class RecoverNavBacktrack(smach.State):
    def __init__(self):
        smach.State.__init__(self,
                             outcomes=['succeeded', 'failure', 'preempted'],
                             input_keys=['goal','n_nav_fails'],
                             output_keys=['goal','n_nav_fails'],
                             )

        self.ptu_action_client = actionlib.SimpleActionClient('/SetPTUState', PtuGotoAction)
        self.move_base_action_client = actionlib.SimpleActionClient('/move_base', MoveBaseAction)
        self.move_base_reconfig_client = dynamic_reconfigure.client.Client('/move_base/DWAPlannerROS')
        
        self.nav_stat=MonitoredNavEvent()
        self.nav_stat.recover_mechanism='backtrack_recovery'

        
        self.BACKTRACK_TRIES=0 #will turn into parameter later
               
                                                  
    def execute(self, userdata):
        
        if self.preempt_requested(): 
            self.service_preempt()
            return 'preempted'

        print "Failures: ", userdata.n_nav_fails
        if userdata.n_nav_fails < self.BACKTRACK_TRIES:
            self.nav_stat.event_start_time=rospy.get_rostime()
            self.nav_stat.event_start_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
            status_msg=rospy.wait_for_message("/monitored_navigation/status", GoalStatusArray , timeout=10.0)
            self.nav_stat.goal_id=status_msg.status_list[0].goal_id
            self.nav_stat.was_helped=False
            self.nav_stat.n_help_requests=userdata.n_nav_fails
            try:
                previous_position = rospy.ServiceProxy('previous_position', PreviousPosition)
                meter_back = previous_position(1.0)
            except rospy.ServiceException, e:
                rospy.logwarn("Couldn't get previous position service, returning failure.")
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat)
                return 'failure'
                
            print "Got the previous position: ", meter_back.previous_pose.pose.position.x, ", ", meter_back.previous_pose.pose.position.y, ", ",  meter_back.previous_pose.pose.position.z
                
            try:
                republish_pointcloud = rospy.ServiceProxy('republish_pointcloud', RepublishPointcloud)
                republish_pointcloud(True, '/head_xtion/depth/points', '/move_base/head_subsampled', 0.05)
            except rospy.ServiceException, e:
                rospy.logwarn("Couldn't get republish pointcloud service, returning failure.")
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat)   
                return 'failure'
                
            print "Managed to republish pointcloud."
                      
            params = { 'max_vel_x' : -0.1, 'min_vel_x' : -0.9 }
            config = self.move_base_reconfig_client.update_configuration(params)
            
            ptu_goal = PtuGotoGoal();
            ptu_goal.pan = -179
            ptu_goal.tilt = 50
            ptu_goal.pan_vel = 1
            ptu_goal.tilt_vel = 1
            self.ptu_action_client.send_goal(ptu_goal)
            self.ptu_action_client.wait_for_result()
            
            if self.preempt_requested():
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat)   
                self.service_preempt()
                return 'preempted'
            
            move_goal = MoveBaseGoal()
            move_goal.target_pose.pose = meter_back.previous_pose.pose
            move_goal.target_pose.header.frame_id = meter_back.previous_pose.header.frame_id
            self.move_base_action_client.cancel_all_goals()
            rospy.sleep(rospy.Duration.from_sec(1))
            #print movegoal
            self.move_base_action_client.send_goal(move_goal)
            self.move_base_action_client.wait_for_result()
            
            if self.preempt_requested():
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat)                 
                self.service_preempt()
                return 'preempted'
            
            params = { 'max_vel_x' : 0.9, 'min_vel_x' : 0.1 }
            config = self.move_base_reconfig_client.update_configuration(params)
            
            ptu_goal = PtuGotoGoal();
            ptu_goal.pan = 0
            ptu_goal.tilt = 0
            ptu_goal.pan_vel = 1
            ptu_goal.tilt_vel = 1
            self.ptu_action_client.send_goal(ptu_goal)
            self.ptu_action_client.wait_for_result()
            
            if self.preempt_requested():
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat) 
                self.service_preempt()
                return 'preempted'
            
            try:
                republish_pointcloud = rospy.ServiceProxy('republish_pointcloud', RepublishPointcloud)
                republish_pointcloud(False, '', '', 0.0)
            except rospy.ServiceException, e:
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat) 
                rospy.logwarn("Couldn't stop republish pointcloud service, returning failure.")
                return 'failure'
                
            print "Reset PTU, move_base parameters and stopped republishing pointcloud."
                
            if self.move_base_action_client.get_state() != GoalStatus.SUCCEEDED: #set the previous goal again
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat) 
                return 'failure'
            else:
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                monitored_navigation.mongo_logger.add_event(self.nav_stat) 
                return 'succeeded'
        else:
            self.nav_stat.event_end_time=rospy.get_rostime()
            self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
            monitored_navigation.mongo_logger.add_event(self.nav_stat) 
            return 'failure'
        
      
            
    
    def service_preempt(self):
        #check if preemption is working
        smach.State.service_preempt(self)



class RecoverNavHelp(smach.State):
    def __init__(self,max_nav_recovery_attempts=5):
        smach.State.__init__(self,
                             # we need the number of move_base fails as
                             # incoming data from the move_base action state,
                             # because it is not possible for this recovery
                             # behaviour to check if it was succeeded
                             outcomes=['succeeded', 'failure', 'preempted'],
                             input_keys=['goal','n_nav_fails'],
                             output_keys=['goal','n_nav_fails'],
                             )

        self.set_nav_thresholds(max_nav_recovery_attempts)
        

        self.enable_motors= rospy.ServiceProxy('enable_motors',
                                                  EnableMotors)
                                                  
        self.ask_help=rospy.ServiceProxy('/monitored_navigation/human_help/manager', AskHelp)
        self.service_msg=AskHelpRequest()
        self.service_msg.failed_component=AskHelpRequest.NAVIGATION
                                                  
        #self.clear_costmap
                                                  

        
        self.being_helped=False
        self.help_finished=False     
        
        self.nav_stat=MonitoredNavEvent()
        self.nav_stat.recover_mechanism='nav_help_recovery'

        
                                                  

   
    def help_offered(self, req):
        self.being_helped=True
        return []
    
        
    def nav_recovered(self,req):
        self.being_helped=False
        self.help_finished=True
        return []
        
                                                  
    def execute(self, userdata):

       
        rospy.sleep(0.2)
        self.isRecovered=False
                
        self.enable_motors(False)    
        # since there is no way to check if the recovery behaviour was
        # successful, we always go back to the move_base action state with
        # 'succeeded' until the number of failures treshold is reached
        if userdata.n_nav_fails < self.MAX_NAV_RECOVERY_ATTEMPTS:
            #self.get_logger().log_navigation_recovery_attempt(success=True,
             #                                                 attempts=userdata.n_move_base_fails)
             
             
           

            rospy.sleep(0.2)
            self.isRecovered=False
                
            self.enable_motors(False)    
            
            self.nav_stat.was_helped=False
            self.nav_stat.event_start_time=rospy.get_rostime()
            self.nav_stat.event_start_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
            status_msg=rospy.wait_for_message("/monitored_navigation/status", GoalStatusArray , timeout=10.0)
            self.nav_stat.goal_id=status_msg.status_list[0].goal_id
            self.nav_stat.n_help_requests=userdata.n_nav_fails
        
            self.help_offered_monitor=rospy.Service('/monitored_navigation/help_offered', Empty, self.help_offered)
            self.help_done_monitor=rospy.Service('/monitored_navigation/help_finished', Empty, self.nav_recovered)            
            
            
            
            
            self.service_msg.interaction_status=AskHelpRequest.ASKING_HELP
            self.service_msg.interaction_service='help_offered'
            
            
            if self.preempt_requested(): 
                self.service_preempt()
                return 'preempted'
                
            try:
                self.ask_help(self.service_msg)
            except rospy.ServiceException, e:
                rospy.logwarn("No means of asking for human help available.")
            
            for i in range(0,40):
                if self.preempt_requested():
                    self.nav_stat.event_end_time=rospy.get_rostime()
                    self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                    
                    monitored_navigation.mongo_logger.add_event(self.nav_stat)
                    self.service_preempt()
                    return 'preempted'
                if self.being_helped:
                    self.nav_stat.was_helped=True
                    self.service_msg.interaction_status=AskHelpRequest.BEING_HELPED
                    self.service_msg.interaction_service='help_finished'
                    try:
                        self.ask_help(self.service_msg)
                    except rospy.ServiceException, e:
                        rospy.logwarn("No means of asking for human help available.")
                    break
                rospy.sleep(1)       
            
            if self.being_helped:
                self.being_helped=False
                for i in range(0,60):
                    #if self.preempt_requested():
                        #self.nav_stat.event_end_time=rospy.get_rostime()
                        #self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        #monitored_navigation.mongo_logger.add_event(self.nav_stat)
                        #self.service_preempt()
                        #return 'preempted'
                    if self.help_finished:
                        self.help_finished=False
                        break
                    rospy.sleep(1)   
            
            self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
            self.service_msg.interaction_service='none'
            try:
                self.ask_help(self.service_msg)
            except rospy.ServiceException, e:
                rospy.logwarn("No means of asking for human help available.")
            self.help_offered_monitor.shutdown()
            self.help_done_monitor.shutdown()
            self.nav_stat.event_end_time=rospy.get_rostime()
            self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
            monitored_navigation.mongo_logger.add_event(self.nav_stat)
            return 'succeeded'
        else:
            userdata.n_nav_fails=0
            self.nav_stat.event_end_time=rospy.get_rostime()
            self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
            monitored_navigation.mongo_logger.add_event(self.nav_stat)
            return 'failure'


            
    def set_nav_thresholds(self, max_nav_recovery_attempts):
        if max_nav_recovery_attempts is not None:
            self.MAX_NAV_RECOVERY_ATTEMPTS = max_nav_recovery_attempts        
            
    
    def service_preempt(self):
        self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
        self.service_msg.interaction_service='none'
        try:
            self.ask_help(self.service_msg)
        except rospy.ServiceException, e:
            rospy.logwarn("No means of asking for human help available.")
        self.help_offered_monitor.shutdown()
        self.help_done_monitor.shutdown()
        smach.State.service_preempt(self)
        
        
            
class RecoverBumper(smach.State):
    def __init__(self,max_bumper_recovery_attempts=5):
        smach.State.__init__(self,
                             outcomes=['succeeded', 'failure', 'preempted']
                             )
        self.reset_motorstop = rospy.ServiceProxy('reset_motorstop',
                                                  ResetMotorStop)
        self.enable_motors= rospy.ServiceProxy('enable_motors',
                                                  EnableMotors)                                          
        self.being_helped = False
        self.help_finished=False
        self.motor_monitor = rospy.Subscriber("/motor_status",
                                              MotorStatus,
                                              self.bumper_monitor_cb)
        
        self.ask_help=rospy.ServiceProxy('/monitored_navigation/human_help/manager', AskHelp)
        self.service_msg=AskHelpRequest()
        self.service_msg.failed_component=AskHelpRequest.BUMPER
        

        
        self.nav_stat=MonitoredNavEvent()
        self.nav_stat.recover_mechanism='bumper_recovery'
 
        
        self.set_nav_thresholds(max_bumper_recovery_attempts)
        
        
        
    
    def help_offered(self, req):
        self.being_helped=True
        return []
    
        
    def bumper_recovered(self,req):
        self.being_helped=False
        self.help_finished=True
        return []
        
    def bumper_monitor_cb(self, msg):
        self.isRecovered = not msg.bumper_pressed

    # restarts the motors and check to see of they really restarted.
    # Between each failure the waiting time to try and restart the motors
    # again increases. This state can check its own success
    def execute(self, userdata):
        
        if self.preempt_requested(): 
            self.service_preempt()
            return 'preempted'
        
        
        self.help_offered_monitor=rospy.Service('/monitored_navigation/help_offered', Empty, self.help_offered)
        self.help_done_monitor=rospy.Service('/monitored_navigation/help_finished', Empty, self.bumper_recovered)
        
        self.nav_stat.event_start_time=rospy.get_rostime()
        self.nav_stat.event_start_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
        status_msg=rospy.wait_for_message("/monitored_navigation/status", GoalStatusArray , timeout=10.0)
        self.nav_stat.goal_id=status_msg.status_list[0].goal_id
        self.nav_stat.was_helped=False
        
        n_tries=1
        while True:
            
            if self.preempt_requested():
                self.nav_stat.event_end_time=rospy.get_rostime()
                self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                self.nav_stat.n_help_requests=n_tries
                monitored_navigation.mongo_logger.add_event(self.nav_stat)
                self.service_preempt()
                return 'preempted'
            if self.being_helped:
                self.nav_stat.was_helped=True
                self.service_msg.interaction_status=AskHelpRequest.BEING_HELPED
                self.service_msg.interaction_service='help_finished'
                try:
                    self.ask_help(self.service_msg)
                except rospy.ServiceException, e:
                    rospy.logwarn("No means of asking for human help available.")
                for i in range(0,60):
                    #if self.preempt_requested():
                        #self.nav_stat.event_end_time=rospy.get_rostime()
                        #self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        #self.nav_stat.n_help_requests=n_tries
                        #monitored_navigation.mongo_logger.add_event(self.nav_stat)
                        #self.service_preempt()
                        #return 'preempted'
                    if self.help_finished:
                        self.being_helped=False
                        break
                    rospy.sleep(1)
                if not self.help_finished:
                    self.being_helped=False
                    self.service_msg.interaction_status=AskHelpRequest.ASKING_HELP
                    self.service_msg.interaction_service='help_offered'
                    try:
                        self.ask_help(self.service_msg) 
                    except rospy.ServiceException, e:
                        rospy.logwarn("No means of asking for human help available.")
            elif self.help_finished:
                self.help_finished=False
                self.reset_motorstop()    
                rospy.sleep(0.1)
                if self.preempt_requested():
                    self.nav_stat.event_end_time=rospy.get_rostime()
                    self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                    self.nav_stat.n_help_requests=n_tries
                    monitored_navigation.mongo_logger.add_event(self.nav_stat)
                    self.service_preempt()
                    return 'preempted'
                if self.isRecovered:
                    self.help_done_monitor.shutdown()
                    self.help_offered_monitor.shutdown()
                    self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
                    self.service_msg.interaction_service='none'
                    try:
                        self.ask_help(self.service_msg)
                    except rospy.ServiceException, e:
                        rospy.logwarn("No means of asking for human help available.")
                        self.nav_stat.event_end_time=rospy.get_rostime()
                        self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        self.nav_stat.n_help_requests=n_tries
                        monitored_navigation.mongo_logger.add_event(self.nav_stat)
                    return 'succeeded' 
                else:
                    self.service_msg.interaction_status=AskHelpRequest.HELP_FAILED
                    self.service_msg.interaction_service='help_offered'
                    try:
                        self.ask_help(self.service_msg)
                    except rospy.ServiceException, e:
                        rospy.logwarn("No means of asking for human help available.")
            else:  
                for i in range(0,4*n_tries):
                    if self.being_helped:
                        break
                    self.enable_motors(False)
                    self.reset_motorstop()
                    rospy.sleep(0.1)
                    if self.preempt_requested():
                        self.nav_stat.event_end_time=rospy.get_rostime()
                        self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        self.nav_stat.n_help_requests=n_tries
                        monitored_navigation.mongo_logger.add_event(self.nav_stat)
                        self.service_preempt()
                        return 'preempted'
                    if self.isRecovered:
                        self.help_done_monitor.shutdown()
                        self.help_offered_monitor.shutdown()
                        self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
                        self.service_msg.interaction_service='none'
                        try:
                            self.ask_help(self.service_msg)
                        except rospy.ServiceException, e:
                            rospy.logwarn("No means of asking for human help available.")
                        self.nav_stat.event_end_time=rospy.get_rostime()
                        self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        self.nav_stat.n_help_requests=n_tries
                        monitored_navigation.mongo_logger.add_event(self.nav_stat)
                        return 'succeeded' 
                    rospy.sleep(1)
                    if n_tries>self.MAX_BUMPER_RECOVERY_ATTEMPTS:
                        self.nav_stat.event_end_time=rospy.get_rostime()
                        self.nav_stat.event_end_pose=rospy.wait_for_message("/robot_pose", Pose , timeout=10.0)
                        self.nav_stat.n_help_requests=n_tries
                        monitored_navigation.mongo_logger.add_event(self.nav_stat)                        
                        return 'failure'
                n_tries += 1
            
       
            
                if n_tries>1:
                    self.service_msg.interaction_status=AskHelpRequest.ASKING_HELP
                    self.service_msg.interaction_service='help_offered'
                    try:
                        self.ask_help(self.service_msg)
                    except rospy.ServiceException, e:
                        rospy.logwarn("No means of asking for human help available.")
	
    def service_preempt(self):
        self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
        self.service_msg.interaction_service='none'
        try:
            self.ask_help(self.service_msg)
        except rospy.ServiceException, e:
            rospy.logwarn("No means of asking for human help available.")
        self.help_offered_monitor.shutdown()
        self.help_done_monitor.shutdown()
        smach.State.service_preempt(self)            
            
   
            
            
    def set_nav_thresholds(self, max_bumper_recovery_attempts):
        if max_bumper_recovery_attempts is not None:
            self.MAX_BUMPER_RECOVERY_ATTEMPTS = max_bumper_recovery_attempts
                

    def service_preempt(self):
        self.service_msg.interaction_status=AskHelpRequest.HELP_FINISHED
        self.service_msg.interaction_service='none'
        try:
            self.ask_help(self.service_msg)
        except rospy.ServiceException, e:
            rospy.logwarn("No means of asking for human help available.")
        self.help_offered_monitor.shutdown()
        self.help_done_monitor.shutdown()
        smach.State.service_preempt(self)           
                
                
                
                
                
class RecoverStuckOnCarpet(smach.State):
    def __init__(self):
        smach.State.__init__(self,
            outcomes    = ['succeeded','failure', 'preempted'])
        self.vel_pub = rospy.Publisher('/cmd_vel', Twist)
        self._vel_cmd = Twist()
        
        


    def execute(self,userdata):
        if self.preempt_requested(): 
            self.service_preempt()
            return 'preempted'
        
        #small forward vel to unstuck robot
        self._vel_cmd.linear.x=0.8
        self._vel_cmd.angular.z=0.4
        for i in range(0,4): 
            self.vel_pub.publish(self._vel_cmd)
            self._vel_cmd.linear.x=self._vel_cmd.linear.x-0.2
            self._vel_cmd.angular.z=self._vel_cmd.angular.z-0.2  
   #         if self.preempt_requested():
   #             self.service_preempt()
   #             return 'preempted'
            rospy.sleep(0.2)
        self._vel_cmd.linear.x=0.0
        self._vel_cmd.angular.z=0.0
        self.vel_pub.publish(self._vel_cmd)
        

        
        #check if behaviour was successful       
        if True:     
            return 'succeeded'
        else:
            return 'failure'
