close all
for i=1:length(NormStd)
    ratio(i,1) = PotMag(i)/NormStd(i);
end

[ZsRatio,ZsRatiomean,ZsRatiostdev] = zscore(ratio);
[ZsGbar,ZsGbaromean,ZsGbarstdev] = zscore(GbarInMax);
[ZsNormLinAccmax,ZsNormAccmaxmean,ZsNormAccmaxstdev] = zscore(LinAccmax);

Xxlsback=[];
j=1;
change=[];
ii=1;
% p=1;
% for i=1:length(LocSpeed)-1
%   if( LocSpeed(i) < 2.78)
%       colnum1(p)=i;
%       p=p+1;
%   end
% end
%  ZsRatio(colnum1)=[];
%  ZsGbar(colnum1)=[];
%  Long(colnum1)=[];
%  Lat(colnum1)=[];
% LocBearing(colnum1)=[];

% [x,y,utmzone] = deg2utm(Long,Lat);
%  p=1;
%  for i=1:length(x)-1
%    dist=((x(i+1)-x(i))^2+(y(i+1)-y(i))^2);
%    difftime = etime(datevec(datestr(TimeofDetection(i+1))),datevec(datestr(TimeofDetection(i))));
%    if(dist/difftime<2.78)
%       colnum1(p)=i;
%        p=p+1;
%    end
%  end
%    ZsRatio(colnum1)=[];
%   ZsGbar(colnum1)=[];
%   Long(colnum1)=[];
%   Lat(colnum1)=[];
%  LocBearing(colnum1)=[];
% 
% 
p=1;
for i=1:length(LocBearing)-1
  if( abs(LocBearing(i)-LocBearing(i+1))>180 || LocBearing(i)==0)
      colnum(p)=i;
      p=p+1;
  end
end
 ZsRatio(colnum)=[];
 ZsGbar(colnum)=[];
 Long(colnum)=[];
 Lat(colnum)=[];
LocBearing(colnum)=[];
for i=1:length(LocBearing)
    if(abs(LocBearing(i)-LocBearing(i+1))>45 && LocBearing(i)~=0)
        change(i,j)=LocBearing(i);
        LocBearing(i,2)=j;
        j=j+1;
    else
         change(i,j)=LocBearing(i);
         LocBearing(i,2)=j;
    end
end
index=LocBearing(:,2);
longclback=[];
latclback=[];
Checkme2=1;
for i=1:j
    Xxls = [ZsRatio(index == i,1),ZsGbar(index == i,1)];
    longcl=Long(index == i,1);
    latcl=Lat(index == i,1);
    Xxls=[Xxls;Xxlsback];
    longcl=[longcl;longclback];
    latcl=[latcl;latclback];
    if(length( Xxls)<9)
       Xxlsback =  Xxls;
       longclback=longcl;
       latclback=latcl;
       %Xxls=[];
    else
        %Xxls = [Xxls;Xxlsback];
        [c,p,NoOfinst,gmmfinal,Checkme2,XGauss]=ClusterCalc(Xxls,longcl,latcl);
        if(Checkme2==1)
        membvalue=[];
        clusters(i).gaussian = gmmfinal;
        for k= 1:2
            membvalue(:,k)=mvnpdf(XGauss(:,[1 2]),gmmfinal.mu(k,:),gmmfinal.Sigma(:,:,k));
        end
        memloc = [XGauss,membvalue];
        filename = ['FinalClusteringGaussTabho',num2str(i),'.csv'];
        dlmwrite(filename,memloc, 'precision', 15)
       Xxlsback=[];
       longclback=[];
       latclback=[];
        else
       Xxlsback =  Xxls;
       longclback=longcl;
       latclback=latcl;
        end

    end
end