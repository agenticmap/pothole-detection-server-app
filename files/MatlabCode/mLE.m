clc; close all; 
format long;
%M = csvread('ACC_06-18-2017-6-5124PM_Final.csv',2,1);
figure;
plot(PotMag,NormStd,'K*','MarkerSize',5);
figure;
plot(PotMag1,NormStd1,'K*','MarkerSize',5);
X=[PotMag,NormStd];
%plot(AccY,GyroY,'K*','MarkerSize',5);
%X=[AccY,GyroY];
%---------------------Kmeans------------------------------
%[idx,C] = kmeans(X,3);
figure;
plot(X(idx==1,1),X(idx==1,2),'r.','MarkerSize',12)
hold on
plot(X(idx==2,1),X(idx==2,2),'b.','MarkerSize',12)
plot(X(idx==3,1),X(idx==3,2),'m.','MarkerSize',12)
plot(C(:,1),C(:,2),'kx',...
     'MarkerSize',15,'LineWidth',3)
legend('Cluster 1','Cluster 2','Centroids',...
       'Location','NW')
title 'Cluster Assignments and Centroids'
hold off
%TrainDataNoPot = [AccX(idx==3,1),AccY(idx==3,1),AccZ(idx==3,1),GyroX(idx==3,1),GyroY(idx==3,1),GyroZ(idx==3,1),LinAccX(idx==3,1),LinAccY(idx==3,1),LinAccZ(idx==3,1),OrientX(idx==3,1),OrientY(idx==3,1),OrientZ(idx==3,1),PotMag(idx==3,1),NormStd(idx==3,1),Long(idx==3,1),Lat(idx==3,1)];
%TrainDataPot = [AccX(idx==2,1),AccY(idx==2,1),AccZ(idx==2,1),GyroX(idx==2,1),GyroY(idx==2,1),GyroZ(idx==2,1),LinAccX(idx==2,1),LinAccY(idx==2,1),LinAccZ(idx==2,1),OrientX(idx==2,1),OrientY(idx==2,1),OrientZ(idx==2,1),PotMag(idx==2,1),NormStd(idx==2,1),Long(idx==2,1),Lat(idx==2,1)];
%TrainDataCrack = [AccX(idx==1,1),AccY(idx==1,1),AccZ(idx==1,1),GyroX(idx==1,1),GyroY(idx==1,1),GyroZ(idx==1,1),LinAccX(idx==1,1),LinAccY(idx==1,1),LinAccZ(idx==1,1),OrientX(idx==1,1),OrientY(idx==1,1),OrientZ(idx==1,1),PotMag(idx==1,1),NormStd(idx==1,1),Long(idx==1,1),Lat(idx==1,1)];

%TrainDataNoPot = [AccX(idx==3,1),AccY(idx==3,1),AccZ(idx==3,1),GyroX(idx==3,1),GyroY(idx==3,1),GyroZ(idx==3,1),LinAccX(idx==3,1),LinAccY(idx==3,1),LinAccZ(idx==3,1),PotMag(idx==3,1),NormStd(idx==3,1),Long(idx==3,1),Lat(idx==3,1)];
TrainNotPot = [AccX(idx==1,1),AccY(idx==1,1),AccZ(idx==1,1),GyroX(idx==1,1),GyroY(idx==1,1),GyroZ(idx==1,1),LinAccX(idx==1,1),LinAccY(idx==1,1),LinAccZ(idx==1,1),PotMag(idx==1,1),NormStd(idx==1,1),Long(idx==1,1),Lat(idx==1,1)];
TrainstdNotPot=[PotMag(idx==1,1),NormStd(idx==1,1)];
for i=1:length(TrainNotPot)
    type(i,:)={'NotPot'};
end
TrainCrack = [AccX(idx==2,1),AccY(idx==2,1),AccZ(idx==2,1),GyroX(idx==2,1),GyroY(idx==2,1),GyroZ(idx==2,1),LinAccX(idx==2,1),LinAccY(idx==2,1),LinAccZ(idx==2,1),PotMag(idx==2,1),NormStd(idx==2,1),Long(idx==2,1),Lat(idx==2,1)];
TrainstdCrack=[PotMag(idx==2,1),NormStd(idx==2,1)];
for i=length(TrainNotPot)+1:length(TrainNotPot)+1+length(TrainCrack)
    type(i,:)={'Crack'};
end
TrainPot = [AccX(idx==3,1),AccY(idx==3,1),AccZ(idx==3,1),GyroX(idx==3,1),GyroY(idx==3,1),GyroZ(idx==3,1),LinAccX(idx==3,1),LinAccY(idx==3,1),LinAccZ(idx==3,1),PotMag(idx==3,1),NormStd(idx==3,1),Long(idx==3,1),Lat(idx==3,1)];
TrainstdPot=[PotMag(idx==3,1),NormStd(idx==3,1)];
for i=length(TrainNotPot)+length(TrainCrack)+1:length(TrainNotPot)+length(TrainCrack)+1+length(TrainPot)
    type(i,:)={'Pot'};
end
%xTrain=[TrainDataPot;TrainDataNoPot];
dlmwrite('TrainNotPot.csv',TrainNotPot, 'precision', 15)
dlmwrite('TrainPot.csv',TrainPot, 'precision', 15)
dlmwrite('TrainCrack.csv',TrainCrack, 'precision', 15)


